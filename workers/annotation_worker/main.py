"""Durable preparation worker: `python -m workers.annotation_worker.main`.

Jobs are claimed with a short `FOR UPDATE SKIP LOCKED` transaction that locks only the job row. Each claim
gets a fresh lease token and generation. Rendering happens outside any transaction, in a subprocess.
Every publication locks drawing row then job row and verifies token, generation and unexpired lease in the
same transaction, so an expired worker can never publish after a replacement claimed the job.
"""
from __future__ import annotations

import json
import logging
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from apps.api.app.annotation.config import settings
from apps.api.app.annotation.database import get_engine, transaction
from apps.api.app.annotation.services.assets import asset_bytes, insert_asset

log = logging.getLogger("annotation.worker")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LostOwnership(Exception):
    pass


class InvalidInput(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class TransientFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Claim:
    job_id: str
    drawing_id: str
    token: str
    generation: int
    attempt: int
    source_format: str
    source_asset_id: str


def claim_job() -> Claim | None:
    token = secrets.token_urlsafe(24)
    with transaction() as conn:
        row = conn.execute(text("""
            UPDATE annotation.preparation_jobs j SET state = 'RUNNING', lease_token = :token,
                lease_generation = j.lease_generation + 1, attempt_count = j.attempt_count + 1,
                leased_until = now() + make_interval(secs => :lease), heartbeat_at = now()
            WHERE j.id = (
                SELECT id FROM annotation.preparation_jobs
                WHERE job_type = 'RENDER' AND (
                    (state = 'QUEUED' AND next_attempt_at <= now()) OR (state = 'RUNNING' AND leased_until < now()))
                ORDER BY next_attempt_at, created_at
                FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING j.id, j.drawing_id, j.lease_generation, j.attempt_count"""),
            {"token": token, "lease": settings.worker_lease_seconds}).mappings().first()
    if row is None:
        return None
    with transaction() as conn:
        drawing = conn.execute(text("SELECT source_format, source_asset_id FROM annotation.drawings WHERE id = :id"),
                               {"id": row["drawing_id"]}).mappings().one()
    return Claim(str(row["id"]), str(row["drawing_id"]), token, int(row["lease_generation"]), int(row["attempt_count"]),
                 drawing["source_format"], str(drawing["source_asset_id"]))


def _lock_owned(conn: Connection, claim: Claim) -> None:
    """Lock drawing then job, and verify this worker still owns an unexpired lease on the job."""
    conn.execute(text("SELECT id FROM annotation.drawings WHERE id = :id FOR UPDATE"), {"id": claim.drawing_id})
    owned = conn.execute(text(
        "SELECT 1 FROM annotation.preparation_jobs WHERE id = :id AND state = 'RUNNING' AND lease_token = :token "
        "AND lease_generation = :gen AND leased_until > now() FOR UPDATE"),
        {"id": claim.job_id, "token": claim.token, "gen": claim.generation}).first()
    if owned is None:
        raise LostOwnership(claim.job_id)


def heartbeat(claim: Claim) -> bool:
    with transaction() as conn:
        result = conn.execute(text(
            "UPDATE annotation.preparation_jobs SET leased_until = now() + make_interval(secs => :lease), "
            "heartbeat_at = now() WHERE id = :id AND lease_token = :token AND lease_generation = :gen "
            "AND state = 'RUNNING' AND leased_until > now()"),
            {"lease": settings.worker_lease_seconds, "id": claim.job_id, "token": claim.token, "gen": claim.generation})
    return result.rowcount == 1


class Heartbeater(threading.Thread):
    """Keeps the job lease alive while a render subprocess is busy."""

    def __init__(self, claim: Claim) -> None:
        super().__init__(daemon=True)
        self.claim, self.stop_event, self.lost = claim, threading.Event(), False

    def run(self) -> None:
        while not self.stop_event.wait(settings.worker_heartbeat_seconds):
            try:
                if not heartbeat(self.claim):
                    self.lost = True
                    return
            except Exception:  # database blip: the lease has slack; try again next tick
                log.warning("job %s heartbeat failed", self.claim.job_id, exc_info=True)


def _run_renderer(args: list[str], timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run([sys.executable, "-m", "workers.annotation_worker.rendering", *args],
                              capture_output=True, text=True, timeout=timeout, cwd=PROJECT_ROOT)
    except subprocess.TimeoutExpired as exc:
        raise TransientFailure("RENDER_TIMEOUT", "Rendering a page took too long.") from exc
    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode == 3:
        data = json.loads(last)
        raise InvalidInput(data["error_code"], data["error_message"])
    if proc.returncode != 0:
        log.warning("renderer exited %s: %s", proc.returncode, proc.stderr[-2000:])
        raise TransientFailure("RENDER_CRASHED", "The page renderer stopped unexpectedly.")
    return json.loads(last)


def _published_pages(conn: Connection, drawing_id: str) -> dict[int, int]:
    rows = conn.execute(text(
        "SELECT p.page_number, a.byte_count FROM annotation.pages p JOIN annotation.assets a ON a.id = p.render_asset_id "
        "WHERE p.drawing_id = :id"), {"id": drawing_id})
    return {r[0]: r[1] for r in rows}


def process(claim: Claim) -> None:
    with transaction() as conn:
        _lock_owned(conn, claim)
        conn.execute(text("UPDATE annotation.drawings SET preparation_state = 'RUNNING' WHERE id = :id"),
                     {"id": claim.drawing_id})
        source = asset_bytes(conn, claim.source_asset_id)
    assert source is not None
    with tempfile.TemporaryDirectory(prefix="annotation-render-") as tmp:
        source_path = Path(tmp) / "source.bin"
        source_path.write_bytes(source)
        page_count = int(_run_renderer(["inspect", str(source_path), claim.source_format], 60)["page_count"])
        if page_count < 1 or page_count > settings.max_pdf_pages:
            raise InvalidInput("PDF_PAGE_LIMIT", f"PDFs are limited to {settings.max_pdf_pages} pages.")
        with transaction() as conn:
            _lock_owned(conn, claim)
            conn.execute(text("UPDATE annotation.drawings SET page_count = :n WHERE id = :id AND "
                              "(page_count IS NULL OR page_count = :n)"), {"n": page_count, "id": claim.drawing_id})
            published = _published_pages(conn, claim.drawing_id)
            _progress(conn, claim, len(published), page_count)
        total_bytes = sum(published.values())
        limits = json.dumps({"dpi": settings.render_dpi, "max_pixels": settings.max_page_pixels,
                             "max_asset_bytes": settings.max_render_asset_bytes,
                             "thumbnail_px": settings.thumbnail_max_px})
        for page_number in range(1, page_count + 1):
            if page_number in published:
                continue  # retries keep published page IDs and coordinate frames
            out_dir = Path(tmp) / f"page-{page_number}"
            out_dir.mkdir()
            meta = _run_renderer(["render", str(source_path), claim.source_format, str(page_number), str(out_dir),
                                  limits, str(settings.render_memory_limit_mib), str(settings.render_timeout_seconds)],
                                 settings.render_timeout_seconds + 10)
            render = (out_dir / "render.png").read_bytes()
            thumb = (out_dir / "thumbnail.png").read_bytes()
            total_bytes += len(render)
            if total_bytes > settings.max_total_render_bytes:
                raise InvalidInput("RENDER_TOTAL_TOO_LARGE", "The rendered pages of this drawing exceed the storage limit.")
            with transaction() as conn:
                _lock_owned(conn, claim)
                render_id = insert_asset(conn, "PAGE_RENDER", "image/png", render, meta["render_profile"])
                thumb_id = insert_asset(conn, "THUMBNAIL", "image/png", thumb, {"page_number": page_number})
                conn.execute(text(
                    "INSERT INTO annotation.pages (id, drawing_id, page_number, coordinate_unit, width, height, "
                    "source_geometry, render_asset_id, thumbnail_asset_id, render_width_px, render_height_px, "
                    "canonical_to_render_matrix, render_profile) VALUES (:id, :drawing, :n, :unit, :w, :h, "
                    "CAST(:geom AS jsonb), :render, :thumb, :pw, :ph, CAST(:matrix AS jsonb), CAST(:profile AS jsonb)) "
                    "ON CONFLICT (drawing_id, page_number) DO NOTHING"),
                    {"id": str(uuid.uuid4()), "drawing": claim.drawing_id, "n": page_number,
                     "unit": meta["coordinate_unit"], "w": meta["width"], "h": meta["height"],
                     "geom": json.dumps(meta["source_geometry"]), "render": render_id, "thumb": thumb_id,
                     "pw": meta["render_width_px"], "ph": meta["render_height_px"],
                     "matrix": json.dumps(meta["canonical_to_render_matrix"]),
                     "profile": json.dumps(meta["render_profile"])})
                _progress(conn, claim, len(_published_pages(conn, claim.drawing_id)), page_count)
            log.info("drawing %s page %s/%s published", claim.drawing_id, page_number, page_count)
    with transaction() as conn:
        _lock_owned(conn, claim)
        count = len(_published_pages(conn, claim.drawing_id))
        if count != page_count:
            raise TransientFailure("PAGES_MISSING", "Some pages were not published.")
        conn.execute(text(
            "UPDATE annotation.preparation_jobs SET state = 'SUCCEEDED', finished_at = now(), leased_until = NULL, "
            "error_code = NULL, error_message = NULL WHERE id = :id"), {"id": claim.job_id})
        # Workflow status never derives from job completion; only technical state changes here.
        conn.execute(text(
            "UPDATE annotation.drawings SET preparation_state = 'READY', page_count = :n, preparation_error_code = NULL, "
            "preparation_error_message = NULL WHERE id = :id"), {"n": page_count, "id": claim.drawing_id})


def _progress(conn: Connection, claim: Claim, done: int, total: int) -> None:
    conn.execute(text(
        "UPDATE annotation.preparation_jobs SET progress = CAST(:p AS jsonb), "
        "run_metadata = run_metadata || CAST(:meta AS jsonb) WHERE id = :id"),
        {"p": json.dumps({"pages_done": done, "page_count": total}), "id": claim.job_id,
         "meta": json.dumps({"render_dpi": settings.render_dpi, "renderer": "annotation-renderer-1"})})


def fail(claim: Claim, code: str, message: str, *, terminal: bool) -> None:
    try:
        with transaction() as conn:
            _lock_owned(conn, claim)
            if not terminal and claim.attempt < settings.worker_max_attempts:
                delay = 5 * 2 ** (claim.attempt - 1)
                conn.execute(text(
                    "UPDATE annotation.preparation_jobs SET state = 'QUEUED', leased_until = NULL, lease_token = NULL, "
                    "next_attempt_at = now() + make_interval(secs => :delay), error_code = :code, "
                    "error_message = :message WHERE id = :id"),
                    {"delay": delay, "code": code, "message": message, "id": claim.job_id})
                conn.execute(text("UPDATE annotation.drawings SET preparation_state = 'QUEUED' WHERE id = :id"),
                             {"id": claim.drawing_id})
                return
            conn.execute(text(
                "UPDATE annotation.preparation_jobs SET state = 'FAILED', leased_until = NULL, lease_token = NULL, "
                "finished_at = now(), error_code = :code, error_message = :message WHERE id = :id"),
                {"code": code, "message": message, "id": claim.job_id})
            conn.execute(text(
                "UPDATE annotation.drawings SET preparation_state = 'FAILED', preparation_error_code = :code, "
                "preparation_error_message = :message WHERE id = :id"),
                {"code": code, "message": message, "id": claim.drawing_id})
    except LostOwnership:
        log.info("job %s was reclaimed; discarding failure", claim.job_id)


def run_claim(claim: Claim) -> None:
    beat = Heartbeater(claim)
    beat.start()
    started = time.monotonic()
    try:
        process(claim)
        log.info("job %s drawing %s prepared in %.1fs", claim.job_id, claim.drawing_id, time.monotonic() - started)
    except LostOwnership:
        log.warning("job %s lost its lease; unpublished output discarded", claim.job_id)
    except InvalidInput as exc:
        log.warning("job %s invalid input %s", claim.job_id, exc.code)
        fail(claim, exc.code, exc.message, terminal=True)
    except TransientFailure as exc:
        log.warning("job %s transient failure %s (attempt %s)", claim.job_id, exc.code, claim.attempt)
        fail(claim, exc.code, exc.message, terminal=False)
    except Exception:
        log.exception("job %s failed unexpectedly", claim.job_id)
        fail(claim, "PREPARATION_ERROR", "Preparation failed unexpectedly.", terminal=False)
    finally:
        beat.stop_event.set()


def run_once() -> bool:
    claim = claim_job()
    if claim is None:
        return False
    run_claim(claim)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    get_engine()
    if settings.enable_ocr_suggestions:
        log.warning("ENABLE_OCR_SUGGESTIONS is set, but OCR suggestions are an optional later phase and are not "
                    "implemented in this version; drawings are prepared for manual annotation only.")
    log.info("annotation worker started (poll %.1fs, concurrency %d)", settings.worker_poll_seconds,
             settings.worker_concurrency)

    def loop() -> None:
        while not stopping.is_set():
            try:
                if run_once():
                    continue
            except Exception:
                log.exception("worker loop error")
            stopping.wait(settings.worker_poll_seconds)

    threads = [threading.Thread(target=loop, name=f"worker-{i}", daemon=True)
               for i in range(max(1, settings.worker_concurrency))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
