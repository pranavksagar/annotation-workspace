"""Canonical page-frame geometry.

Every page has one canonical frame: origin top-left, x right, y down. PDF pages use
effective points (UserUnit applied) of the displayed, cropped and rotated page; images use
pixels after EXIF orientation normalization. Region quadrilaterals are clockwise in this
frame, starting at the first corner in reading orientation.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from .canonical import round_coordinate

BOUNDARY_EPSILON = 0.01
MIN_AREA = 1e-6


class GeometryError(ValueError):
    pass


def _finite_pair(point: object) -> tuple[float, float]:
    if (not isinstance(point, (list, tuple)) or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)):
        raise GeometryError("Each vertex must be a pair of numbers")
    x, y = float(point[0]), float(point[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise GeometryError("Coordinates must be finite")
    return x, y


def signed_area(polygon: Sequence[Sequence[float]]) -> float:
    """Shoelace area; positive means clockwise in a y-down frame."""
    return sum(p[0] * q[1] - q[0] * p[1] for p, q in zip(polygon, list(polygon[1:]) + [polygon[0]])) / 2


def normalize_quad(polygon: object, width: float, height: float,
                   epsilon: float = BOUNDARY_EPSILON) -> list[list[float]]:
    """Validate a region quadrilateral and return its canonical serialization.

    Rejects anything that is not four distinct finite vertices forming a clockwise, convex,
    non-self-intersecting quadrilateral with positive area inside the page. Vertices up to
    `epsilon` outside the page are clamped to the page; larger errors are rejected.
    """
    if not isinstance(polygon, (list, tuple)) or len(polygon) != 4:
        raise GeometryError("A region needs exactly four vertices")
    points = [_finite_pair(p) for p in polygon]
    clamped: list[list[float]] = []
    for x, y in points:
        if x < -epsilon or y < -epsilon or x > width + epsilon or y > height + epsilon:
            raise GeometryError("Region vertices must lie inside the page")
        clamped.append([round_coordinate(min(max(x, 0.0), width)), round_coordinate(min(max(y, 0.0), height))])
    if len({tuple(p) for p in clamped}) != 4:
        raise GeometryError("Region vertices must be distinct")
    crosses = []
    for i in range(4):
        a, b, c = clamped[i], clamped[(i + 1) % 4], clamped[(i + 2) % 4]
        crosses.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if any(value <= 0 for value in crosses):
        # Every turn must be clockwise (positive in y-down coordinates). This also rules out
        # self-intersection and collinear vertices for a four-vertex polygon.
        raise GeometryError("Region must be a convex clockwise quadrilateral")
    if signed_area(clamped) <= MIN_AREA:
        raise GeometryError("Region must have positive area")
    return clamped


def bounding_box(polygon: Iterable[Sequence[float]]) -> tuple[float, float, float, float]:
    pts = list(polygon)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def apply_affine(matrix: Sequence[Sequence[float]], point: Sequence[float]) -> tuple[float, float]:
    """Apply a 2x3 affine matrix [[a, b, c], [d, e, f]] to (x, y)."""
    (a, b, c), (d, e, f) = matrix
    return a * point[0] + b * point[1] + c, d * point[0] + e * point[1] + f


def scale_matrix(sx: float, sy: float) -> list[list[float]]:
    return [[sx, 0.0, 0.0], [0.0, sy, 0.0]]


def pdf_point_to_canonical(x: float, y: float, crop: Iterable[float], rotation: int = 0,
                           user_unit: float = 1.0) -> list[float]:
    """Map PDF user space to the canonical frame (adapted from ingestor's geometry)."""
    x0, y0, x1, y1 = [float(value) for value in crop]
    ux, uy = (float(x) - x0) * user_unit, (float(y) - y0) * user_unit
    width, height = (x1 - x0) * user_unit, (y1 - y0) * user_unit
    angle = int(rotation) % 360
    if angle == 0:
        return [ux, height - uy]
    if angle == 90:
        return [uy, ux]
    if angle == 180:
        return [width - ux, uy]
    if angle == 270:
        return [height - uy, width - ux]
    raise GeometryError("PDF page rotation must be a multiple of 90 degrees")


def pdf_canonical_size(crop: Iterable[float], rotation: int = 0, user_unit: float = 1.0) -> tuple[float, float]:
    x0, y0, x1, y1 = [float(value) for value in crop]
    width, height = abs(x1 - x0) * user_unit, abs(y1 - y0) * user_unit
    return (height, width) if int(rotation) % 180 else (width, height)


def region_crop_transform(polygon: Sequence[Sequence[float]], matrix: Sequence[Sequence[float]]) -> dict:
    """Describe the oriented crop for a quad in render pixels.

    Returns the four render-pixel corners (reading-order first corner, clockwise), plus the
    output width/height of a rectified crop. A recognizer should perspective-warp exactly
    these corners, never the axis-aligned bounding box of a rotated region.
    """
    corners = [apply_affine(matrix, p) for p in polygon]
    width = (math.dist(corners[0], corners[1]) + math.dist(corners[3], corners[2])) / 2
    height = (math.dist(corners[0], corners[3]) + math.dist(corners[1], corners[2])) / 2
    return {"corners": [list(c) for c in corners], "width": max(1, round(width)), "height": max(1, round(height))}
