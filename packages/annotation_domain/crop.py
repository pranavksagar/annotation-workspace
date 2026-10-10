"""Rectify an oriented quad out of a page render. Shared by training adapters and OCR suggestions."""
from __future__ import annotations

import io
from typing import Sequence


def crop_quad(render_png: bytes, corners_px: Sequence[Sequence[float]], width: int, height: int):
    """Warp the four render-pixel corners (reading-order first corner, clockwise) to a width x height image."""
    from PIL import Image

    with Image.open(io.BytesIO(render_png)) as image:
        c0, c1, c2, c3 = corners_px
        # PIL QUAD order: upper-left, lower-left, lower-right, upper-right of the source quadrilateral.
        data = [*c0, *c3, *c2, *c1]
        return image.transform((width, height), Image.Transform.QUAD, data, resample=Image.Resampling.BICUBIC)
