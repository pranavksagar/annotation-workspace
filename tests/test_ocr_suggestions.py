"""OCR suggestions: a read-only text guess for a box. Nothing is stored."""
from __future__ import annotations

import dataclasses
import io
import shutil

import pytest

from conftest import prepare, rect

TESSERACT = shutil.which("tesseract") is not None


def text_png(text: str, size=(500, 200), at=(40, 40), font_size=48) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", size, (255, 255, 255))
    ImageDraw.Draw(image).text(at, text, fill=(0, 0, 0), font=ImageFont.load_default(size=font_size))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture()
def ocr_on(monkeypatch):
    from apps.api.app.annotation.config import settings
    from apps.api.app.annotation.services import drawings, ocr
    enabled = dataclasses.replace(settings, enable_ocr_suggestions=True)
    monkeypatch.setattr(ocr, "settings", enabled)
    monkeypatch.setattr(drawings, "settings", enabled)


def suggest(client, detail, polygon, page_id=None):
    page_id = page_id or detail["pages"][0]["id"]
    return client.post(f"/api/annotation/drawings/{detail['drawing']['id']}/ocr-suggestions",
                       json={"page_id": page_id, "polygon": polygon})


def test_disabled_by_default(client):
    detail = prepare(client, text_png("62.565"), "d.png")
    assert detail["ocr_enabled"] is False
    response = suggest(client, detail, rect(30, 30, 300, 80))
    assert response.status_code == 404 and response.json()["error"]["code"] == "OCR_DISABLED"


def test_detail_reports_capability(client, ocr_on):
    assert prepare(client, text_png("1"), "d.png")["ocr_enabled"] is True


@pytest.mark.skipif(not TESSERACT, reason="tesseract is not installed")
def test_suggests_dimension_text(client, ocr_on):
    detail = prepare(client, text_png("62.565"), "d.png")
    response = suggest(client, detail, rect(30, 30, 300, 80))
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "62.565"
    assert response.json()["confidence"] > 30


@pytest.mark.skipif(not TESSERACT, reason="tesseract is not installed")
def test_blank_box_gives_empty_suggestion(client, ocr_on):
    detail = prepare(client, text_png("62.565"), "d.png")
    response = suggest(client, detail, rect(300, 140, 150, 40))
    assert response.status_code == 200 and response.json()["text"] == ""


def test_engine_is_mocked_with_crop_geometry(client, ocr_on, monkeypatch):
    from apps.api.app.annotation.services import ocr
    seen = {}

    def fake(image):
        seen["size"] = image.size
        return {"text": "Ø20", "confidence": 91.0}
    monkeypatch.setattr(ocr, "_recognize", fake)
    detail = prepare(client, text_png("x"), "d.png")
    response = suggest(client, detail, rect(30, 30, 100, 40))
    assert response.status_code == 200 and response.json() == {"text": "Ø20", "confidence": 91.0}
    # 100x40 crop scaled to a 64px-high line plus a 16px margin on each side.
    assert seen["size"][1] == 64 + 32


def test_validation_errors(client, ocr_on, monkeypatch):
    from apps.api.app.annotation.services import ocr
    monkeypatch.setattr(ocr, "_recognize", lambda image: {"text": "", "confidence": 0.0})
    detail = prepare(client, text_png("x"), "d.png")
    unknown = "00000000-0000-4000-8000-000000000000"
    assert suggest(client, detail, rect(10, 10, 50, 20), page_id=unknown).status_code == 404
    bad = suggest(client, detail, rect(-50, 10, 50, 20))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_GEOMETRY"
    assert suggest(client, detail, [[0, 0], [1, 1]]).status_code == 422
    missing = client.post(f"/api/annotation/drawings/{unknown}/ocr-suggestions",
                          json={"page_id": unknown, "polygon": rect(1, 1, 5, 5)})
    assert missing.status_code == 404


def test_engine_missing_is_reported(client, ocr_on, monkeypatch):
    from apps.api.app.annotation.services import ocr
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    detail = prepare(client, text_png("x"), "d.png")
    response = suggest(client, detail, rect(30, 30, 100, 40))
    assert response.status_code == 503 and response.json()["error"]["code"] == "OCR_UNAVAILABLE"


def test_oversized_crop_rejected(client, ocr_on, monkeypatch):
    from apps.api.app.annotation.services import ocr
    monkeypatch.setattr(ocr, "settings", dataclasses.replace(ocr.settings, ocr_max_crop_pixels=100))
    detail = prepare(client, text_png("x"), "d.png")
    response = suggest(client, detail, rect(30, 30, 100, 40))
    assert response.status_code == 422 and response.json()["error"]["code"] == "OCR_REGION_TOO_LARGE"


def test_suggestion_writes_nothing(client, ocr_on, monkeypatch, engine):
    from sqlalchemy import text
    from apps.api.app.annotation.services import ocr
    monkeypatch.setattr(ocr, "_recognize", lambda image: {"text": "5", "confidence": 99.0})
    detail = prepare(client, text_png("5"), "d.png")
    before = client.get(f"/api/annotation/drawings/{detail['drawing']['id']}/document").json()
    assert suggest(client, detail, rect(30, 30, 100, 40)).status_code == 200
    after = client.get(f"/api/annotation/drawings/{detail['drawing']['id']}/document").json()
    assert before == after and after["regions"] == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.mutation_receipts")).scalar() == 0


@pytest.mark.parametrize("raw,expected", [
    ("-Q.190", "-0.190"), ("62,565", "62.565"), ("O.5x45°", "0.5x45°"), ("113.00", "113.00"),
    ("center", "center"), ("Quote", "Quote"), ("A–B", "A–B"), ("–0.5", "-0.5"),
])
def test_clean_only_touches_numeric_tokens(raw, expected):
    from apps.api.app.annotation.services.ocr import _clean
    assert _clean(raw) == expected
