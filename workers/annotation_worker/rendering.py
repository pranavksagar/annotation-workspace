"""Page rendering in an isolated subprocess.

Run as `python -m workers.annotation_worker.rendering <command> ...`; the worker invokes it with
memory/time limits so a renderer crash or runaway page cannot take down the worker or the API.

Canonical frames:
- PDF: displayed page (CropBox, /Rotate applied, UserUnit applied), top-left origin, points.
- PNG/JPEG: EXIF-orientation-normalized image, top-left origin, pixels.
The canonical-to-render matrix is computed from the actual output bitmap dimensions.
"""
from __future__ import annotations

import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

RENDERER_VERSION = "annotation-renderer-1"


class InvalidInput(Exception):
    """The source cannot be prepared without an operational fix; not retried automatically."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RenderLimits:
    dpi: float = 300
    max_pixels: int = 64_000_000
    max_asset_bytes: int = 40 * 1024 * 1024
    thumbnail_px: int = 320


def _pdf_page_geometry(reader_page) -> dict:
    from pypdf.generic import ArrayObject  # noqa: F401  (import check)

    def box(name: str):
        value = reader_page.get(name)
        return [float(v) for v in value] if value is not None else None

    media = [float(v) for v in reader_page.mediabox]
    crop = [float(v) for v in reader_page.cropbox]
    rotation = int(reader_page.get("/Rotate", 0) or 0) % 360
    user_unit = float(reader_page.get("/UserUnit", 1) or 1)
    if rotation % 90:
        raise InvalidInput("UNSUPPORTED_PAGE_TRANSFORM", "A page uses a rotation that is not a multiple of 90°.")
    if not (math.isfinite(user_unit) and user_unit > 0):
        raise InvalidInput("UNSUPPORTED_PAGE_TRANSFORM", "A page has an invalid UserUnit.")
    # pdfium renders the CropBox clipped to the MediaBox.
    x0, y0 = max(min(crop[0], crop[2]), min(media[0], media[2])), max(min(crop[1], crop[3]), min(media[1], media[3]))
    x1, y1 = min(max(crop[0], crop[2]), max(media[0], media[2])), min(max(crop[1], crop[3]), max(media[1], media[3]))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise InvalidInput("UNSUPPORTED_PAGE_TRANSFORM", "A page has an empty crop box.")
    effective = [x0, y0, x1, y1]
    width, height = (x1 - x0) * user_unit, (y1 - y0) * user_unit
    if rotation in (90, 270):
        width, height = height, width
    return {"kind": "PDF", "media_box": media, "crop_box": crop, "effective_crop_box": effective,
            "rotation": rotation, "user_unit": user_unit, "trim_box": box("/TrimBox"), "width": width,
            "height": height}


def inspect(source: bytes, source_format: str) -> dict:
    if source_format == "PDF":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(source), strict=False)
            if reader.is_encrypted:
                raise InvalidInput("PDF_ENCRYPTED", "This PDF is encrypted.")
            return {"page_count": len(reader.pages)}
        except InvalidInput:
            raise
        except Exception as exc:
            raise InvalidInput("FILE_MALFORMED", "This PDF could not be read.") from exc
    return {"page_count": 1}


def _encode_png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def _thumbnail(image, max_px: int) -> bytes:
    thumb = image.copy()
    thumb.thumbnail((max_px, max_px))
    return _encode_png(thumb)


def render_page(source: bytes, source_format: str, page_number: int, limits: RenderLimits) -> tuple[dict, bytes, bytes]:
    """Render one page; returns (metadata, render_png, thumbnail_png)."""
    if source_format == "PDF":
        return _render_pdf_page(source, page_number, limits)
    return _render_image(source, source_format, limits)


def _render_pdf_page(source: bytes, page_number: int, limits: RenderLimits):
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(source), strict=False)
    geometry = _pdf_page_geometry(reader.pages[page_number - 1])
    width, height = geometry["width"], geometry["height"]
    dpi = float(limits.dpi)
    if width * height * (dpi / 72) ** 2 > limits.max_pixels:
        # Renderers round dimensions up, so leave a small margin under the pixel limit.
        dpi = math.floor(72 * math.sqrt(limits.max_pixels / (width * height)) * 0.995 * 100) / 100
    try:
        pdf = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        raise InvalidInput("FILE_MALFORMED", "This PDF could not be rendered.") from exc
    try:
        page = pdf[page_number - 1]
        # pdfium ignores UserUnit, so scale per PDF unit includes it to hit the target real-world DPI.
        scale = dpi / 72 * geometry["user_unit"]
        bitmap = page.render(scale=scale, rotation=0, draw_annots=True, may_draw_forms=True)
        image = bitmap.to_pil().convert("RGB")
        page.close()
    finally:
        pdf.close()
    px_w, px_h = image.size
    expected_w, expected_h = width * dpi / 72, height * dpi / 72
    if abs(px_w - expected_w) > 2 or abs(px_h - expected_h) > 2:
        raise InvalidInput("UNSUPPORTED_PAGE_TRANSFORM",
                           f"Page {page_number} rendered at an unexpected size; its coordinate frame cannot be trusted.")
    render = _encode_png(image)
    if len(render) > limits.max_asset_bytes:
        raise InvalidInput("RENDER_TOO_LARGE", f"Page {page_number} is too large to store as an image.")
    meta = {
        "coordinate_unit": "PT", "width": width, "height": height, "render_width_px": px_w,
        "render_height_px": px_h, "canonical_to_render_matrix": [[px_w / width, 0.0, 0.0], [0.0, px_h / height, 0.0]],
        "source_geometry": geometry,
        "render_profile": {"renderer": RENDERER_VERSION, "engine": f"pdfium/pypdfium2 {pdfium.V_PYPDFIUM2}",
                           "requested_dpi": float(limits.dpi), "effective_dpi": dpi, "format": "PNG"},
    }
    return meta, render, _thumbnail(image, limits.thumbnail_px)


def _render_image(source: bytes, source_format: str, limits: RenderLimits):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = limits.max_pixels * 4
    try:
        with Image.open(io.BytesIO(source)) as opened:
            orientation = opened.getexif().get(0x0112, 1)
            image = ImageOps.exif_transpose(opened)
            image.load()
    except Exception as exc:
        raise InvalidInput("FILE_MALFORMED", "This image could not be read.") from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    width, height = image.size
    render_image, effective_scale = image, 1.0
    if width * height > limits.max_pixels:
        effective_scale = math.sqrt(limits.max_pixels / (width * height))
        render_image = image.resize((max(1, int(width * effective_scale)), max(1, int(height * effective_scale))),
                                    Image.Resampling.LANCZOS)
    px_w, px_h = render_image.size
    render = _encode_png(render_image)
    if len(render) > limits.max_asset_bytes:
        raise InvalidInput("RENDER_TOO_LARGE", "This image is too large to store as a page render.")
    meta = {
        "coordinate_unit": "PX", "width": float(width), "height": float(height), "render_width_px": px_w,
        "render_height_px": px_h, "canonical_to_render_matrix": [[px_w / width, 0.0, 0.0], [0.0, px_h / height, 0.0]],
        "source_geometry": {"kind": source_format, "exif_orientation": int(orientation), "normalized_width": width,
                            "normalized_height": height},
        "render_profile": {"renderer": RENDERER_VERSION, "engine": "Pillow", "scale": effective_scale,
                           "format": "PNG"},
    }
    return meta, render, _thumbnail(render_image, limits.thumbnail_px)


def _set_limits(memory_mib: int, seconds: int) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 5))
        if sys.platform.startswith("linux"):  # RLIMIT_AS is not enforced reliably on macOS
            limit = memory_mib * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass


def main(argv: list[str]) -> int:
    """Subprocess protocol: source path in, files + JSON line out. Exit 3 means invalid input."""
    command, source_path, source_format = argv[0], Path(argv[1]), argv[2]
    try:
        source = source_path.read_bytes()
        if command == "inspect":
            print(json.dumps(inspect(source, source_format)))
            return 0
        page_number, out_dir = int(argv[3]), Path(argv[4])
        limits = RenderLimits(**json.loads(argv[5]))
        _set_limits(int(argv[6]), int(argv[7]))
        meta, render, thumb = render_page(source, source_format, page_number, limits)
        (out_dir / "render.png").write_bytes(render)
        (out_dir / "thumbnail.png").write_bytes(thumb)
        print(json.dumps(meta))
        return 0
    except InvalidInput as exc:
        print(json.dumps({"error_code": exc.code, "error_message": exc.message}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
