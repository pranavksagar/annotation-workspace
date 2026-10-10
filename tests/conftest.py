"""Integration tests run against real PostgreSQL (TEST_DATABASE_URL), never SQLite."""
from __future__ import annotations

import io
import os
import uuid

import pytest

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://annotation:annotation@127.0.0.1:55432/annotation")
TEST_DB = os.getenv("TEST_DATABASE_NAME", "annotation_test")
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
os.environ["DATABASE_URL"] = TEST_URL
os.environ.setdefault("RENDER_DPI", "72")
os.environ.setdefault("TRUSTED_HOSTS", "testserver,127.0.0.1,localhost")

import psycopg  # noqa: E402
from sqlalchemy import text  # noqa: E402

TABLES = ("legacy_import_map, mutation_receipts, browser_progress, editing_leases, group_members, requirement_groups, regions, "
          "preparation_jobs, training_snapshots, pages, drawings, assets")


def _create_database() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    from alembic import command
    from alembic.config import Config
    root = os.path.dirname(os.path.dirname(__file__))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "migrations/annotation"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def database():
    _create_database()
    yield


@pytest.fixture(autouse=True)
def clean(database):
    from apps.api.app.annotation.database import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join('annotation.' + t.strip() for t in TABLES.split(','))} CASCADE"))
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from apps.api.app.annotation_main import app
    return TestClient(app)


@pytest.fixture()
def engine():
    from apps.api.app.annotation.database import get_engine
    return get_engine()


# ---------------------------------------------------------------- fixtures for sources

def make_pdf(pages: int = 1, size=(612, 792), rotate: int = 0, crop=None, user_unit: float | None = None,
             marker=None, label: str = "") -> bytes:
    """Generated PDF. `marker` draws a filled square at PDF user-space (x, y, side)."""
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=size)
    for index in range(pages):
        c.setFont("Helvetica", 12)
        c.drawString(40, size[1] - 60, f"{label} page {index + 1} {uuid.uuid4().hex[:6]}")
        if marker:
            x, y, side = marker
            c.rect(x, y, side, side, stroke=0, fill=1)
        c.showPage()
    c.save()
    reader = PdfReader(io.BytesIO(buffer.getvalue()))
    writer = PdfWriter()
    for page in reader.pages:
        if rotate:
            page[NameObject("/Rotate")] = NumberObject(rotate)
        if crop:
            page[NameObject("/CropBox")] = ArrayObject([FloatObject(v) for v in crop])
        if user_unit:
            page[NameObject("/UserUnit")] = FloatObject(user_unit)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_png(size=(400, 300), color=(255, 255, 255), fmt="PNG", exif_orientation: int | None = None,
             marker=None) -> bytes:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, color)
    if marker:
        x, y, side = marker
        ImageDraw.Draw(image).rectangle([x, y, x + side - 1, y + side - 1], fill=(0, 0, 0))
    out = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def upload(client, data: bytes, name: str = "drawing.pdf"):
    return client.post("/api/annotation/drawings", files={"file": (name, data, "application/octet-stream")})


def prepare(client, data: bytes | None = None, name: str = "drawing.pdf", pages: int = 1) -> dict:
    """Upload and run the worker until the drawing is READY."""
    from workers.annotation_worker.main import run_once
    response = upload(client, data or make_pdf(pages=pages), name)
    assert response.status_code in (200, 201), response.text
    drawing_id = response.json()["drawing"]["id"]
    for _ in range(5):
        if not run_once():
            break
    detail = client.get(f"/api/annotation/drawings/{drawing_id}").json()
    assert detail["drawing"]["preparation_state"] == "READY", detail
    return detail


class Editor:
    """A browser tab editing one drawing."""

    def __init__(self, client, drawing_id: str, browser_id: str | None = None) -> None:
        self.client, self.drawing_id = client, drawing_id
        self.browser_id, self.tab_id = browser_id or str(uuid.uuid4()), str(uuid.uuid4())
        self.token = self.generation = None
        self.version = "0"

    @property
    def base(self) -> str:
        return f"/api/annotation/drawings/{self.drawing_id}"

    def acquire(self):
        response = self.client.post(f"{self.base}/edit-session", json={"browser_id": self.browser_id, "tab_id": self.tab_id})
        if response.status_code == 200:
            body = response.json()
            self.token, self.generation, self.version = body["lease_token"], body["lease_generation"], body["annotation_version"]
        return response

    def envelope(self, operations, mutation_id=None, expected=None):
        return {"mutation_id": mutation_id or str(uuid.uuid4()), "expected_annotation_version": expected or self.version,
                "browser_id": self.browser_id, "tab_id": self.tab_id, "lease_token": self.token or "x" * 20,
                "lease_generation": self.generation or "0", "operations": operations}

    def mutate(self, operations, mutation_id=None, expected=None):
        response = self.client.post(f"{self.base}/mutations", json=self.envelope(operations, mutation_id, expected))
        if response.status_code == 200:
            self.version = response.json()["annotation_version"]
        return response

    def complete(self, coverage, mutation_id=None, policy="mechanical-labeling-v1"):
        response = self.client.post(f"{self.base}/complete", json={
            "mutation_id": mutation_id or str(uuid.uuid4()), "expected_annotation_version": self.version,
            "browser_id": self.browser_id, "tab_id": self.tab_id, "lease_token": self.token,
            "lease_generation": self.generation, "labeling_policy_version": policy, "coverage": coverage})
        if response.status_code == 200:
            self.version = response.json()["annotation_version"]
        return response


def rect(x: float, y: float, w: float, h: float) -> list[list[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def region_op(page_id: str, polygon=None, **fields) -> dict:
    region = {"id": fields.pop("id", str(uuid.uuid4())), "page_id": page_id, "kind": fields.pop("kind", "TEXT"),
              "polygon": polygon or rect(10, 10, 50, 20), **fields}
    return {"type": "region.create", "region": region}
