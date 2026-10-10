#!/usr/bin/env python3
"""End-to-end smoke check against a running stack: upload → render → annotate → resume → complete → training reader.

It uses the public HTTP API exactly as the browser does, then reads the completed snapshot through the internal
training reader (DATABASE_URL). It adds one generated drawing named smoke-check-<id>.pdf to the library.

  DATABASE_URL=... .venv/bin/python scripts/smoke-check.py --api http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import io
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pdf_bytes() -> bytes:
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(842, 595))
    c.drawString(340, 310, f"Ø20 +0.25 {uuid.uuid4().hex[:8]}")
    c.showPage()
    c.save()
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    base = args.api.rstrip("/") + "/api/annotation"
    http = httpx.Client(timeout=60)

    def step(name: str) -> None:
        print(f"• {name}", flush=True)

    step("health")
    assert http.get(f"{base}/health/ready").status_code == 200, "API is not ready (migrations applied?)"
    step("upload")
    upload = http.post(f"{base}/drawings", files={"file": (f"smoke-check-{uuid.uuid4().hex[:6]}.pdf", pdf_bytes(), "application/pdf")})
    assert upload.status_code == 201, upload.text
    drawing_id = upload.json()["drawing"]["id"]
    step("render (waiting for the preparation worker)")
    deadline = time.monotonic() + args.timeout
    while True:
        detail = http.get(f"{base}/drawings/{drawing_id}").json()
        state = detail["drawing"]["preparation_state"]
        if state == "READY":
            break
        assert state != "FAILED", detail["preparation"]
        assert time.monotonic() < deadline, "preparation did not finish; is the worker running?"
        time.sleep(1)
    page = detail["pages"][0]
    assert http.get(f"{base}/assets/{page['render_asset_id']}").status_code == 200

    step("annotate")
    browser, tab = str(uuid.uuid4()), str(uuid.uuid4())
    lease = http.post(f"{base}/drawings/{drawing_id}/edit-session", json={"browser_id": browser, "tab_id": tab}).json()
    region, group = str(uuid.uuid4()), str(uuid.uuid4())
    w, h = page["width"], page["height"]
    quad = [[w * 0.4, h * 0.45], [w * 0.5, h * 0.45], [w * 0.5, h * 0.5], [w * 0.4, h * 0.5]]
    envelope = {"mutation_id": str(uuid.uuid4()), "expected_annotation_version": "0", "browser_id": browser, "tab_id": tab,
                "lease_token": lease["lease_token"], "lease_generation": lease["lease_generation"], "operations": [
                    {"type": "region.create", "region": {"id": region, "page_id": page["id"], "kind": "TEXT", "polygon": quad,
                                                         "transcription": "Ø20"}},
                    {"type": "group.create", "group": {"id": group, "primary_page_id": page["id"], "requirement_type": "DIMENSION"}},
                    {"type": "member.add", "group_id": group, "region_id": region, "role": "NOMINAL", "order_index": 0}]}
    saved = http.post(f"{base}/drawings/{drawing_id}/mutations", json=envelope)
    assert saved.status_code == 200, saved.text
    http.request("DELETE", f"{base}/drawings/{drawing_id}/edit-session",
                 json={"lease_token": lease["lease_token"], "lease_generation": lease["lease_generation"]})

    step("resume from another browser")
    other_browser, other_tab = str(uuid.uuid4()), str(uuid.uuid4())
    document = http.get(f"{base}/drawings/{drawing_id}/document").json()
    assert document["annotation_status"] == "IN_PROGRESS" and document["regions"][0]["transcription"] == "Ø20"
    lease = http.post(f"{base}/drawings/{drawing_id}/edit-session", json={"browser_id": other_browser, "tab_id": other_tab}).json()

    step("complete")
    completed = http.post(f"{base}/drawings/{drawing_id}/complete", json={
        "mutation_id": str(uuid.uuid4()), "expected_annotation_version": document["annotation_version"],
        "browser_id": other_browser, "tab_id": other_tab, "lease_token": lease["lease_token"],
        "lease_generation": lease["lease_generation"], "labeling_policy_version": "mechanical-labeling-v1",
        "coverage": [{"page_id": page["id"], "status": "ANNOTATED"}]})
    assert completed.status_code == 200, completed.text
    snapshot_id = completed.json()["snapshot_id"]

    step("training reader")
    from apps.api.app.annotation.database import get_engine
    from packages.annotation_training.adapters import iter_recognition_examples, iter_requirement_examples
    from packages.annotation_training.reader import list_ready_snapshot_ids, load_asset_bytes, load_snapshot
    with get_engine().connect() as conn:
        assert uuid.UUID(snapshot_id) in list_ready_snapshot_ids(conn)
        snapshot = load_snapshot(conn, snapshot_id)
        load_asset_bytes(conn, snapshot.pages[0]["render_asset_id"], snapshot.pages[0]["render_sha256"])
        assert [e.target_text for e in iter_recognition_examples(snapshot)] == ["Ø20"]
        assert [e.label for e in iter_requirement_examples(snapshot)] == ["POSITIVE"]
    print(f"OK — drawing {drawing_id}, snapshot {snapshot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
