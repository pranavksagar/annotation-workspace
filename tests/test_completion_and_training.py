import io
import threading
import uuid

from PIL import Image
from sqlalchemy import text

from packages.annotation_training.adapters import (crop_region, iter_detection_examples, iter_recognition_examples,
                                                   iter_requirement_examples)
from packages.annotation_training.reader import (SnapshotIntegrityError, list_ready_snapshot_ids, load_asset_bytes,
                                                 load_snapshot)
from tests.conftest import Editor, make_png, prepare, rect, region_op


def annotated(client, data=None, name="drawing.pdf", pages=1):
    detail = prepare(client, data, name, pages=pages)
    drawing_id, page_ids = detail["drawing"]["id"], [p["id"] for p in detail["pages"]]
    editor = Editor(client, drawing_id)
    assert editor.acquire().status_code == 200
    return drawing_id, page_ids, editor


def coverage(page_ids, empty=()):
    return [{"page_id": p, "status": "NO_LABELABLE_CONTENT" if p in empty else "ANNOTATED"} for p in page_ids]


def test_completion_validation_reports_focusable_details(client, engine):
    drawing_id, (page,), editor = annotated(client)
    region, symbol, group = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    editor.mutate([region_op(page, id=region, transcription=""), region_op(page, rect(80, 80, 10, 10), id=symbol, kind="SYMBOL"),
                   {"type": "group.create", "group": {"id": group, "primary_page_id": page, "requirement_type": "THREAD"}}])
    response = editor.complete(coverage([page]))
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "COMPLETION_INVALID"
    found = {(d["code"], d.get("region_id") or d.get("group_id"), d.get("field")) for d in error["details"]}
    assert ("INCOMPLETE_LABEL", region, "transcription") in found
    assert ("INCOMPLETE_LABEL", symbol, "symbol_code") in found
    assert ("INCOMPLETE_REQUIREMENT", group, None) in found
    assert all(d.get("page_id") == page for d in error["details"])
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.training_snapshots")).scalar() == 0
        assert conn.execute(text("SELECT annotation_status FROM annotation.drawings")).scalar() == "IN_PROGRESS"


def test_empty_pages_require_explicit_confirmation(client):
    drawing_id, pages, editor = annotated(client, pages=2)
    editor.mutate([region_op(pages[0], transcription="TITLE")])
    assert editor.complete(coverage(pages)).json()["error"]["details"][0]["code"] == "EMPTY_PAGE_UNCONFIRMED"
    assert editor.complete(coverage(pages, empty=pages)).status_code == 422  # labeled page declared empty
    assert editor.complete(coverage(pages[:1])).status_code == 422  # missing page
    assert editor.complete(coverage(pages, empty=[pages[1]])).status_code == 200


def test_untouched_drawing_cannot_complete_accidentally(client):
    drawing_id, (page,), editor = annotated(client)
    assert editor.complete(coverage([page])).status_code == 422
    assert editor.complete(coverage([page], empty=[page])).status_code == 200  # explicit engineer confirmation


def full_drawing(client, editor, page):
    ids = {k: str(uuid.uuid4()) for k in ("nominal", "upper", "title", "unclassified", "ignore", "sym", "group", "ctx")}
    editor.mutate([
        region_op(page, rect(100, 100, 40, 12), id=ids["nominal"], transcription="Ø20"),
        region_op(page, rect(142, 98, 30, 6), id=ids["upper"], transcription="+0.25"),
        region_op(page, rect(300, 700, 120, 20), id=ids["title"], transcription="DRG-1234",
                  semantic_disposition="NON_REQUIREMENT"),
        region_op(page, rect(300, 650, 120, 20), id=ids["unclassified"], transcription="SCALE 1:1"),
        region_op(page, rect(50, 50, 20, 20), id=ids["ignore"], kind="IGNORE", legibility="UNREADABLE"),
        region_op(page, rect(200, 200, 12, 12), id=ids["sym"], kind="SYMBOL", symbol_code="GDT_FLATNESS"),
        region_op(page, rect(400, 400, 50, 10), id=ids["ctx"], transcription="rif"),
        {"type": "group.create", "group": {"id": ids["group"], "primary_page_id": page, "requirement_type": "DIMENSION"}},
        {"type": "member.add", "group_id": ids["group"], "region_id": ids["nominal"], "role": "NOMINAL"},
        {"type": "member.add", "group_id": ids["group"], "region_id": ids["upper"], "role": "UPPER_TOLERANCE",
         "order_index": 1},
        {"type": "member.add", "group_id": ids["group"], "region_id": ids["ctx"], "role": "CONTEXT", "order_index": 2},
    ])
    return ids


def test_finish_creates_one_snapshot_atomically_and_reader_sees_it(client, engine):
    drawing_id, (page,), editor = annotated(client)
    ids = full_drawing(client, editor, page)
    mutation_id = str(uuid.uuid4())
    response = editor.complete(coverage([page]), mutation_id=mutation_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["annotation_status"] == "COMPLETED" and body["snapshot_sequence"] == 1
    # Uncertain completion retry returns the same snapshot, not a duplicate.
    editor.version = body["previous_annotation_version"]
    retry = editor.complete(coverage([page]), mutation_id=mutation_id)
    assert retry.json()["snapshot_id"] == body["snapshot_id"] and retry.json()["replayed"] is True
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.training_snapshots")).scalar() == 1
        lease = conn.execute(text("SELECT token_hash, expires_at <= now() FROM annotation.editing_leases")).one()
        assert lease[0] == "" and lease[1] is True
        # Stale save after completion cannot change completed data.
    editor.version = body["annotation_version"]
    assert editor.mutate([region_op(page)]).json()["error"]["code"] == "DRAWING_COMPLETED"
    assert editor.acquire().json()["error"]["code"] == "DRAWING_COMPLETED"

    with engine.connect() as conn:
        assert list_ready_snapshot_ids(conn) == [uuid.UUID(body["snapshot_id"])]
        snapshot = load_snapshot(conn, body["snapshot_id"])
        assert snapshot.payload_sha256 == body["payload_sha256"]
        assert "lease" not in str(snapshot.payload) and "center_x" not in str(snapshot.payload)
        detection = list(iter_detection_examples(snapshot))
        assert len(detection) == 1 and len(detection[0].ignore_polygons_px) == 1
        assert len(detection[0].positives) == 6
        recognition = {e.region_id: e for e in iter_recognition_examples(snapshot)}
        assert ids["ignore"] not in recognition
        assert recognition[ids["nominal"]].target_text == "Ø20"
        assert recognition[ids["sym"]].target_text == "⏥" and recognition[ids["sym"]].symbol_code == "GDT_FLATNESS"
        requirement = {e.region_id: e.label for e in iter_requirement_examples(snapshot)}
        assert requirement[ids["nominal"]] == "POSITIVE" and requirement[ids["upper"]] == "POSITIVE"
        assert requirement[ids["title"]] == "NEGATIVE"
        assert ids["unclassified"] not in requirement  # unclassified text is never a semantic negative
        assert ids["ctx"] not in requirement  # CONTEXT alone is not a positive
        render = load_asset_bytes(conn, snapshot.pages[0]["render_asset_id"], snapshot.pages[0]["render_sha256"])
        assert render.startswith(b"\x89PNG")


def test_reopen_removes_from_ready_set_and_preserves_pinned_snapshot(client, engine):
    drawing_id, (page,), editor = annotated(client)
    full_drawing(client, editor, page)
    first = editor.complete(coverage([page])).json()
    with engine.connect() as conn:
        pinned = list_ready_snapshot_ids(conn)
    reopen_id = str(uuid.uuid4())
    body = {"mutation_id": reopen_id, "expected_annotation_version": first["annotation_version"],
            "browser_id": editor.browser_id, "tab_id": editor.tab_id}
    response = client.post(f"/api/annotation/drawings/{drawing_id}/reopen", json=body)
    assert response.status_code == 200 and response.json()["annotation_status"] == "IN_PROGRESS"
    assert client.post(f"/api/annotation/drawings/{drawing_id}/reopen", json=body).json()["replayed"] is True
    with engine.connect() as conn:
        assert list_ready_snapshot_ids(conn) == []
        old = load_snapshot(conn, pinned[0])  # pinned historical snapshot still loads with its checksum
        assert old.payload_sha256 == first["payload_sha256"]
    doc = client.get(f"/api/annotation/drawings/{drawing_id}/document").json()
    assert len(doc["regions"]) == 7  # annotation rows preserved
    assert editor.acquire().status_code == 200
    editor.mutate([region_op(page, rect(500, 500, 20, 10), transcription="NEW")])
    second = editor.complete(coverage([page])).json()
    assert second["snapshot_sequence"] == 2 and second["snapshot_id"] != first["snapshot_id"]


def test_completion_race_with_stale_save(client):
    drawing_id, (page,), editor = annotated(client)
    editor.mutate([region_op(page, transcription="A")])
    stale_version = editor.version
    assert editor.complete(coverage([page])).status_code == 200
    stale = editor.mutate([region_op(page, transcription="B")], expected=stale_version)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "DRAWING_COMPLETED"


def test_concurrent_completion_attempts_create_one_snapshot(client, engine):
    drawing_id, (page,), editor = annotated(client)
    editor.mutate([region_op(page, transcription="A")])
    results = []

    def go():
        results.append(editor.complete(coverage([page]), mutation_id=str(uuid.uuid4())).status_code)

    threads = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(200) == 1
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.training_snapshots")).scalar() == 1


def test_snapshot_rows_are_immutable_and_tampering_is_detected(client, engine):
    drawing_id, (page,), editor = annotated(client)
    editor.mutate([region_op(page, transcription="A")])
    snapshot_id = editor.complete(coverage([page])).json()["snapshot_id"]
    with engine.begin() as conn:
        try:
            conn.execute(text("UPDATE annotation.training_snapshots SET payload = '{}'::jsonb"))
            raise AssertionError("snapshot update should fail")
        except Exception as exc:
            assert "immutable" in str(exc)
    with engine.begin() as conn:
        try:
            conn.execute(text("UPDATE annotation.assets SET mime_type = 'x'"))
            raise AssertionError("asset update should fail")
        except Exception as exc:
            assert "immutable" in str(exc)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE annotation.training_snapshots DISABLE TRIGGER snapshots_immutable"))
        conn.execute(text("UPDATE annotation.training_snapshots SET payload = jsonb_set(payload, '{regions,0,transcription}', '\"B\"')"))
        conn.execute(text("ALTER TABLE annotation.training_snapshots ENABLE TRIGGER snapshots_immutable"))
    with engine.connect() as conn:
        try:
            load_snapshot(conn, snapshot_id)
            raise AssertionError("tampering should be detected")
        except SnapshotIntegrityError:
            pass


def test_rotated_region_crop_matches_render(client, engine):
    # White image with a black bar rotated 90°: a vertical region with reading angle 90.
    image = Image.new("RGB", (200, 200), "white")
    image.paste((0, 0, 0), (90, 20, 110, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    drawing_id, (page,), editor = annotated(client, buffer.getvalue(), "bar.png")
    # Reading orientation rotated 90° clockwise: first corner is the top-right of the bar.
    quad = [[110, 20], [110, 180], [90, 180], [90, 20]]
    region = str(uuid.uuid4())
    assert editor.mutate([region_op(page, quad, id=region, transcription="BAR", reading_angle_degrees=90)]).status_code == 200
    snapshot_id = editor.complete(coverage([page])).json()["snapshot_id"]
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, snapshot_id)
        example = next(iter_recognition_examples(snapshot))
        crop = crop_region(load_asset_bytes(conn, example.render_asset_id), example)
    assert crop.size == (160, 20)  # long axis becomes the reading direction
    pixels = list(crop.convert("L").getdata())
    assert sum(pixels) / len(pixels) < 20  # entirely inside the bar, no neighbouring white


def test_png_upload_used_for_image_page_frames(client):
    detail = prepare(client, make_png(size=(640, 480)), "p.png")
    page = detail["pages"][0]
    assert (page["width"], page["height"], page["coordinate_unit"]) == (640, 480, "PX")
