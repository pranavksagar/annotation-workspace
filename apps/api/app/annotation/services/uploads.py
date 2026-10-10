"""Upload validation by file signature and parser compatibility, not filename extension."""
from __future__ import annotations

import io
import re
import unicodedata
import warnings
from dataclasses import dataclass

from ..config import settings
from ..errors import ApiError

PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


@dataclass
class ValidatedSource:
    source_format: str
    mime_type: str
    page_count: int | None


def sanitize_filename(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip().strip(".")
    base = re.sub(r"\s+", " ", base)
    return (base or "drawing")[:255]


def detect_format(head: bytes) -> str | None:
    # PDFs may carry a short preamble before the header; the PDF spec tolerates up to 1 KiB.
    if PDF_MAGIC in head[:1024]:
        return "PDF"
    if head.startswith(PNG_MAGIC):
        return "PNG"
    if head.startswith(JPEG_MAGIC):
        return "JPEG"
    return None


def validate_source(data: bytes) -> ValidatedSource:
    if not data:
        raise ApiError(422, "FILE_EMPTY", "The uploaded file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise ApiError(413, "UPLOAD_TOO_LARGE",
                       f"Files larger than {settings.max_upload_bytes // (1024 * 1024)} MiB are not accepted.")
    source_format = detect_format(data[:1024])
    if source_format is None:
        raise ApiError(415, "UNSUPPORTED_FILE",
                       "Upload a PDF, PNG or JPEG drawing. Native CAD files (DWG, DXF, STEP) are not supported yet; "
                       "export the drawing to PDF first.")
    if source_format == "PDF":
        return _validate_pdf(data)
    return _validate_image(data, source_format)


def _validate_pdf(data: bytes) -> ValidatedSource:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise ApiError(422, "PDF_ENCRYPTED",
                               "This PDF is password-protected or encrypted. Export an unprotected copy and upload that.")
            page_count = len(reader.pages)
    except ApiError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError, RecursionError) as exc:
        raise ApiError(422, "FILE_MALFORMED", "This PDF could not be read. It may be damaged; export it again.") from exc
    if page_count < 1:
        raise ApiError(422, "FILE_MALFORMED", "This PDF has no pages.")
    if page_count > settings.max_pdf_pages:
        raise ApiError(413, "PDF_PAGE_LIMIT", f"PDFs are limited to {settings.max_pdf_pages} pages.")
    return ValidatedSource("PDF", "application/pdf", page_count)


def _validate_image(data: bytes, source_format: str) -> ValidatedSource:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = settings.max_page_pixels * 4
    try:
        with Image.open(io.BytesIO(data)) as image:
            expected = "PNG" if source_format == "PNG" else "JPEG"
            if image.format != expected:
                raise ApiError(415, "UNSUPPORTED_FILE", "The file contents do not match a supported image format.")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except ApiError:
        raise
    except Image.DecompressionBombError as exc:
        raise ApiError(413, "IMAGE_TOO_LARGE", "This image has too many pixels to prepare.") from exc
    except Exception as exc:  # Pillow raises many exception types for damaged files
        raise ApiError(422, "FILE_MALFORMED", "This image could not be read. It may be damaged.") from exc
    return ValidatedSource(source_format, "image/png" if source_format == "PNG" else "image/jpeg", 1)
