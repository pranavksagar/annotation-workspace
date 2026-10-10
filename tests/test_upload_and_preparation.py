import io
import threading

from sqlalchemy import text

from tests.conftest import make_pdf, make_png, prepare, upload
from workers.annotation_worker import main as worker


def test_pdf_upload_creates_todo_drawing_and_queued_job(client, engine):
    response = upload(client, make_pdf(pages=3), "../../etc/Shaft A.pdf")
    assert response.status_code == 201
    drawing = response.json()["drawing"]
    assert drawing["annotation_status"] == "TO_DO"
    assert drawing["annotation_version"] == "0"
    assert drawing["filename"] == "Shaft A.pdf"
    assert drawing["preparation_state"] == "QUEUED"
    with engine.begin() as conn:
        assert conn.execute(text("SELECT state FROM annotation.preparation_jobs")).scalar() == "QUEUED"


def test_multi_page_pdf_and_image_render_from_database(client, engine):
    detail = prepare(client, make_pdf(pages=3), "three.pdf")
    assert detail["drawing"]["page_count"] == 3
    assert [p["page_number"] for p in detail["pages"]] == [1, 2, 3]
    assert detail["drawing"]["annotation_status"] == "TO_DO"  # preparation never changes workflow status
    png = prepare(client, make_png(), "photo.png")
    assert png["pages"][0]["coordinate_unit"] == "PX"
    asset = client.get(f"/api/annotation/assets/{detail['pages'][0]['render_asset_id']}")
    assert asset.status_code == 200 and asset.headers["content-type"] == "image/png"
    assert "private" in asset.headers["cache-control"]
    again = client.get(f"/api/annotation/assets/{detail['pages'][0]['render_asset_id']}",
                       headers={"If-None-Match": asset.headers["etag"]})
    assert again.status_code == 304


def test_jpeg_upload(client):
    detail = prepare(client, make_png(fmt="JPEG"), "scan.jpg")
    assert detail["drawing"]["source_format"] == "JPEG"


def test_exact_duplicate_returns_existing_without_reset(client):
    data = make_pdf()
    first = upload(client, data).json()["drawing"]
    second = upload(client, data, "renamed.pdf")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["message"] == "This drawing is already in the library"
    assert second.json()["drawing"]["id"] == first["id"]


def test_racing_duplicates_create_one_drawing(client, engine):
    data = make_pdf()
    results = []

    def go():
        results.append(upload(client, data).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count(201) == 1 and sorted(results).count(200) == 3
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.drawings")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM annotation.assets WHERE kind = 'SOURCE'")).scalar() == 1


def test_rejects_unsupported_corrupt_and_encrypted_files(client):
    assert upload(client, b"AC1032 fake dwg", "part.dwg").json()["error"]["code"] == "UNSUPPORTED_FILE"
    assert upload(client, b"%PDF-1.7\n garbage").json()["error"]["code"] == "FILE_MALFORMED"
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf())))
    writer.encrypt("secret")
    out = io.BytesIO()
    writer.write(out)
    response = upload(client, out.getvalue())
    assert response.status_code == 422 and response.json()["error"]["code"] == "PDF_ENCRYPTED"
    assert upload(client, b"\x89PNG\r\n\x1a\nbroken", "x.png").json()["error"]["code"] == "FILE_MALFORMED"


def test_oversized_upload_rejected(client, monkeypatch):
    from apps.api.app.annotation import config
    from apps.api.app.annotation import router
    from apps.api.app.annotation.services import uploads
    small = config.Settings(max_upload_bytes=500)
    monkeypatch.setattr(uploads, "settings", small)
    monkeypatch.setattr(router, "settings", small)
    response = upload(client, make_pdf())
    assert response.status_code == 413 and response.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


def test_stale_worker_cannot_publish_and_retry_keeps_page_ids(client, engine, monkeypatch):
    response = upload(client, make_pdf(pages=2))
    drawing_id = response.json()["drawing"]["id"]
    claim = worker.claim_job()
    assert claim is not None
    # Simulate the lease expiring and a replacement worker claiming the job.
    with engine.begin() as conn:
        conn.execute(text("UPDATE annotation.preparation_jobs SET leased_until = now() - interval '1 second'"))
    replacement = worker.claim_job()
    assert replacement is not None and replacement.generation == claim.generation + 1
    worker.run_claim(claim)  # the stale worker must discard its output
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.pages")).scalar() == 0
    worker.run_claim(replacement)
    detail = client.get(f"/api/annotation/drawings/{drawing_id}").json()
    assert detail["drawing"]["preparation_state"] == "READY"


def test_failed_preparation_is_visible_and_retry_preserves_published_frames(client, engine, monkeypatch):
    drawing_id = upload(client, make_pdf(pages=2)).json()["drawing"]["id"]
    real = worker._run_renderer
    calls = {"n": 0}

    def flaky(args, timeout):
        if args[0] == "render" and args[3] == "2":
            raise worker.InvalidInput("RENDER_TOO_LARGE", "Page 2 is too large to store as an image.")
        return real(args, timeout)

    monkeypatch.setattr(worker, "_run_renderer", flaky)
    worker.run_once()
    detail = client.get(f"/api/annotation/drawings/{drawing_id}").json()
    assert detail["drawing"]["preparation_state"] == "FAILED"
    assert detail["drawing"]["annotation_status"] == "TO_DO"  # no fourth workflow status
    assert detail["preparation"]["error_code"] == "RENDER_TOO_LARGE"
    first_page_id = detail["pages"][0]["id"]
    monkeypatch.setattr(worker, "_run_renderer", real)
    assert client.post(f"/api/annotation/drawings/{drawing_id}/retry-preparation").status_code == 200
    worker.run_once()
    detail = client.get(f"/api/annotation/drawings/{drawing_id}").json()
    assert detail["drawing"]["preparation_state"] == "READY"
    assert detail["pages"][0]["id"] == first_page_id and len(detail["pages"]) == 2
    _ = calls


def test_transient_failure_requeues_with_backoff(client, engine, monkeypatch):
    drawing_id = upload(client, make_pdf()).json()["drawing"]["id"]

    def crash(args, timeout):
        raise worker.TransientFailure("RENDER_CRASHED", "boom")

    monkeypatch.setattr(worker, "_run_renderer", crash)
    worker.run_once()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT state, attempt_count, next_attempt_at > now() FROM annotation.preparation_jobs")).one()
    assert tuple(row) == ("QUEUED", 1, True)
    assert client.get(f"/api/annotation/drawings/{drawing_id}").json()["drawing"]["preparation_state"] == "QUEUED"


def test_library_search_filters_counts_and_pagination(client):
    for index in range(5):
        upload(client, make_pdf(label=f"n{index}"), f"bracket-{index}.pdf")
    upload(client, make_pdf(label="other"), "housing.pdf")
    listing = client.get("/api/annotation/drawings", params={"q": "bracket", "limit": 2}).json()
    assert listing["counts"]["TO_DO"] == 5 and listing["counts"]["ALL"] == 5
    assert len(listing["items"]) == 2 and listing["next_cursor"]
    seen = [i["id"] for i in listing["items"]]
    cursor = listing["next_cursor"]
    while cursor:
        page = client.get("/api/annotation/drawings", params={"q": "bracket", "limit": 2, "cursor": cursor}).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 5
    assert client.get("/api/annotation/drawings", params={"status": "COMPLETED"}).json()["items"] == []
    assert client.get("/api/annotation/drawings", params={"q": "%"}).json()["items"] == []


def test_health(client):
    assert client.get("/api/annotation/health/live").json() == {"status": "ok"}
    assert client.get("/api/annotation/health/ready").status_code == 200


def test_origin_check_accepts_configured_local_origins_only(client):
    for origin in ("http://127.0.0.1:5174", "http://localhost:5174"):
        assert client.get("/api/annotation/health/live").status_code == 200
        response = client.post("/api/annotation/drawings/00000000-0000-4000-8000-000000000000/retry-preparation",
                               headers={"Origin": origin})
        assert response.status_code == 404  # passed the origin check
    rejected = client.post("/api/annotation/drawings/00000000-0000-4000-8000-000000000000/retry-preparation",
                           headers={"Origin": "http://evil.example"})
    assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "ORIGIN_REJECTED"
