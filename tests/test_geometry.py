import math

import pytest

from packages.annotation_domain.canonical import canonical_dumps, canonical_sha256
from packages.annotation_domain.geometry import (GeometryError, normalize_quad, pdf_canonical_size,
                                                 pdf_point_to_canonical, region_crop_transform)


def test_valid_quad_is_rounded_and_preserved():
    quad = normalize_quad([[1.1234567, 2], [10, 2], [10, 8], [1.1234567, 8]], 100, 100)
    assert quad[0] == [1.123457, 2.0]


@pytest.mark.parametrize("polygon", [
    [[0, 0], [10, 0], [10, 10]],                          # three points
    [[0, 0], [10, 0], [10, 10], [10, 10]],                # repeated vertex
    [[0, 0], [0, 10], [10, 10], [10, 0]],                 # counter-clockwise
    [[0, 0], [10, 10], [10, 0], [0, 10]],                 # self-intersecting bow tie
    [[0, 0], [5, 0], [10, 0], [5, 5]],                    # collinear
    [[0, 0], [10, 0], [2, 2], [0, 10]],                   # concave
    [[0, 0], [float("nan"), 0], [10, 10], [0, 10]],       # non-finite
    [[0, 0], [200, 0], [200, 10], [0, 10]],               # out of page
])
def test_invalid_quads_are_rejected(polygon):
    with pytest.raises(GeometryError):
        normalize_quad(polygon, 100, 100)


def test_boundary_epsilon_clamps_small_overshoot_only():
    assert normalize_quad([[-0.005, 0], [100.005, 0], [100, 10], [0, 10]], 100, 100)[0] == [0.0, 0.0]
    with pytest.raises(GeometryError):
        normalize_quad([[-0.02, 0], [100, 0], [100, 10], [0, 10]], 100, 100)


@pytest.mark.parametrize("rotation,expected", [(0, [10, 692]), (90, [100, 10]), (180, [602, 100]), (270, [692, 602])])
def test_pdf_rotation_mapping(rotation, expected):
    assert pdf_point_to_canonical(10, 100, [0, 0, 612, 792], rotation) == pytest.approx(expected)


def test_user_unit_scales_canonical_frame():
    assert pdf_canonical_size([0, 0, 100, 50], 0, 2.0) == (200, 100)
    assert pdf_canonical_size([0, 0, 100, 50], 90, 2.0) == (100, 200)


def test_rotated_crop_uses_oriented_corners():
    angle = math.radians(30)
    cx, cy, w, h = 50, 50, 40, 10
    ux, uy, vx, vy = math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle)
    quad = [[cx - ux * w / 2 - vx * h / 2, cy - uy * w / 2 - vy * h / 2],
            [cx + ux * w / 2 - vx * h / 2, cy + uy * w / 2 - vy * h / 2],
            [cx + ux * w / 2 + vx * h / 2, cy + uy * w / 2 + vy * h / 2],
            [cx - ux * w / 2 + vx * h / 2, cy - uy * w / 2 + vy * h / 2]]
    crop = region_crop_transform(quad, [[2, 0, 0], [0, 2, 0]])
    assert (crop["width"], crop["height"]) == (80, 20)


def test_canonical_json_is_order_independent_and_rejects_nan():
    assert canonical_sha256({"b": 1, "a": "Ø20"}) == canonical_sha256({"a": "Ø20", "b": 1})
    assert "Ø20" in canonical_dumps({"a": "Ø20"})
    with pytest.raises(ValueError):
        canonical_dumps({"a": float("inf")})
