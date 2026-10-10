"""Page frames line up with renders for rotated, cropped, UserUnit PDFs and EXIF-oriented images."""
import io

import pytest
from PIL import Image

from packages.annotation_domain.geometry import apply_affine, pdf_point_to_canonical
from tests.conftest import make_pdf, make_png
from workers.annotation_worker.rendering import InvalidInput, RenderLimits, render_page


def dark_at(png: bytes, x: float, y: float) -> bool:
    image = Image.open(io.BytesIO(png)).convert("L")
    return image.getpixel((int(x), int(y))) < 60


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", [None, [50, 100, 500, 700]])
@pytest.mark.parametrize("user_unit", [None, 2.0])
def test_pdf_marker_lands_at_canonical_coordinates(rotate, crop, user_unit):
    marker = (300, 400, 30)  # PDF user space square
    data = make_pdf(rotate=rotate, crop=crop, user_unit=user_unit, marker=marker)
    meta, render, thumb = render_page(data, "PDF", 1, RenderLimits(dpi=72))
    uu = user_unit or 1.0
    effective_crop = crop or [0, 0, 612, 792]
    expected_w = (effective_crop[2] - effective_crop[0]) * uu
    expected_h = (effective_crop[3] - effective_crop[1]) * uu
    if rotate in (90, 270):
        expected_w, expected_h = expected_h, expected_w
    assert meta["width"] == pytest.approx(expected_w) and meta["height"] == pytest.approx(expected_h)
    center = pdf_point_to_canonical(marker[0] + marker[2] / 2, marker[1] + marker[2] / 2, effective_crop, rotate, uu)
    px = apply_affine(meta["canonical_to_render_matrix"], center)
    assert dark_at(render, *px), (rotate, crop, user_unit, px)
    # A point well outside the marker is white.
    outside = apply_affine(meta["canonical_to_render_matrix"],
                           pdf_point_to_canonical(marker[0] - 20, marker[1] - 20, effective_crop, rotate, uu))
    assert not dark_at(render, *outside)
    assert Image.open(io.BytesIO(thumb)).size[0] <= 320


def test_pixel_limit_lowers_effective_dpi():
    meta, render, _ = render_page(make_pdf(), "PDF", 1, RenderLimits(dpi=300, max_pixels=1_000_000))
    assert meta["render_width_px"] * meta["render_height_px"] <= 1_000_000
    assert meta["render_profile"]["effective_dpi"] < 300
    assert meta["canonical_to_render_matrix"][0][0] == pytest.approx(meta["render_width_px"] / 612)


def test_encoded_size_limit_is_explicit_error():
    with pytest.raises(InvalidInput) as error:
        render_page(make_pdf(), "PDF", 1, RenderLimits(dpi=72, max_asset_bytes=100))
    assert error.value.code == "RENDER_TOO_LARGE"


@pytest.mark.parametrize("orientation,expected_size", [(1, (400, 300)), (6, (300, 400)), (8, (300, 400)), (3, (400, 300))])
def test_exif_orientation_is_normalized(orientation, expected_size):
    data = make_png(size=(400, 300), fmt="JPEG", exif_orientation=orientation, marker=(0, 0, 40))
    meta, render, _ = render_page(data, "JPEG", 1, RenderLimits())
    assert (meta["width"], meta["height"]) == expected_size
    assert meta["source_geometry"]["exif_orientation"] == orientation
    # The marker at the stored top-left corner follows the normalized orientation.
    corners = {1: (5, 5), 3: (expected_size[0] - 5, expected_size[1] - 5), 6: (expected_size[0] - 5, 5),
               8: (5, expected_size[1] - 5)}
    assert dark_at(render, *corners[orientation])
