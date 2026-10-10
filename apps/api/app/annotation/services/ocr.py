"""Read-only OCR suggestions for a box. Nothing is stored: the client decides whether to use the text."""
from __future__ import annotations

import re
import shutil
import threading
from typing import Any

from sqlalchemy import text

from packages.annotation_domain.crop import crop_quad
from packages.annotation_domain.geometry import region_crop_transform

from ..config import settings
from ..database import snapshot_read
from ..errors import ApiError, not_found
from ..repository import get_drawing
from ..validation import normalize_polygon
from .assets import asset_bytes

_slots = threading.BoundedSemaphore(settings.ocr_concurrency)
_BUSY_WAIT_SECONDS = 5
# First pass is restricted to what dimensions look like; the open pass catches words such as "center distance".
_DIMENSION_CHARS = "0123456789.,+-±°Ø/x×"
_MIN_CONFIDENCE = 30.0
_GOOD_CONFIDENCE = 60.0
_TARGET_HEIGHT_PX = 64
_NUMERIC_TOKEN = re.compile(r"^[0-9.,+\-±°ØQOo/x×]*[0-9][0-9.,+\-±°ØQOo/x×]*$")


def suggest(drawing_id: str, page_id: str, polygon: list[list[float]]) -> dict[str, Any]:
    if not settings.enable_ocr_suggestions:
        raise ApiError(404, "OCR_DISABLED", "OCR suggestions are not enabled.")
    with snapshot_read() as conn:
        drawing = get_drawing(conn, drawing_id)
        if drawing["preparation_state"] != "READY":
            raise ApiError(409, "PREPARATION_NOT_READY", "This drawing is still being prepared.")
        page = conn.execute(text(
            "SELECT id, width, height, render_asset_id, canonical_to_render_matrix FROM annotation.pages "
            "WHERE id = :page AND drawing_id = :drawing"), {"page": page_id, "drawing": drawing_id}).mappings().first()
    if page is None:
        raise not_found("Page")
    quad = normalize_polygon(polygon, dict(page), page_id=page_id)
    crop = region_crop_transform(quad, page["canonical_to_render_matrix"])
    if crop["width"] * crop["height"] > settings.ocr_max_crop_pixels:
        raise ApiError(422, "OCR_REGION_TOO_LARGE", "This box is too large to read automatically.")
    if not _slots.acquire(timeout=_BUSY_WAIT_SECONDS):
        raise ApiError(503, "OCR_BUSY", "Text recognition is busy. Try again in a moment.")
    try:
        with snapshot_read() as conn:
            png = asset_bytes(conn, str(page["render_asset_id"]))
        if png is None:
            raise not_found("Asset")
        image = _prepare(crop_quad(png, crop["corners"], crop["width"], crop["height"]))
        return _recognize(image)
    finally:
        _slots.release()


def _prepare(image):
    """Grayscale, normalise contrast, scale small crops up and pad with white (tesseract needs a margin)."""
    from PIL import Image, ImageOps

    gray = ImageOps.autocontrast(image.convert("L"))
    if gray.height < _TARGET_HEIGHT_PX:
        scale = _TARGET_HEIGHT_PX / gray.height
        gray = gray.resize((max(1, round(gray.width * scale)), _TARGET_HEIGHT_PX), Image.Resampling.LANCZOS)
    return ImageOps.expand(gray, border=16, fill=255)


def _recognize(image) -> dict[str, Any]:
    if shutil.which("tesseract") is None:
        raise ApiError(503, "OCR_UNAVAILABLE", "The text recognition engine is not installed.")
    dimension = _run(image, f"-c tessedit_char_whitelist={_DIMENSION_CHARS}")
    best = dimension
    if dimension[1] < _GOOD_CONFIDENCE:
        open_pass = _run(image, "")
        if open_pass[1] > dimension[1]:
            best = open_pass
    value, confidence = best
    if confidence < _MIN_CONFIDENCE:
        value = ""
    return {"text": value, "confidence": round(confidence, 1)}


def _run(image, extra: str) -> tuple[str, float]:
    import pytesseract

    config = f"--psm 7 {extra}".strip()
    try:
        data = pytesseract.image_to_data(image, lang=settings.ocr_language, config=config,
                                         output_type=pytesseract.Output.DICT, timeout=settings.ocr_timeout_seconds)
    except RuntimeError as exc:  # pytesseract raises RuntimeError("Tesseract process timeout")
        raise ApiError(503, "OCR_TIMEOUT", "Text recognition took too long.") from exc
    except pytesseract.TesseractError as exc:
        raise ApiError(503, "OCR_UNAVAILABLE", "Text recognition failed.") from exc
    words, confidences = [], []
    for word, conf in zip(data["text"], data["conf"]):
        word = word.strip()
        if word and float(conf) >= 0:
            words.append(word)
            confidences.append(float(conf))
    if not words:
        return "", 0.0
    value = " ".join(_clean(word) for word in words)
    return value, sum(confidences) / len(confidences)


def _clean(word: str) -> str:
    """Fix the usual confusions, but only inside tokens that already look like numbers."""
    dashed = word.replace("—", "-").replace("–", "-")
    if not _NUMERIC_TOKEN.match(dashed):
        return word
    word = dashed
    word = re.sub(r"(?<=\d),(?=\d)", ".", word)  # drawings here use a decimal point
    return re.sub(r"[QOo]", "0", word)
