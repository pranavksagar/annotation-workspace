import threading
import time
import uuid

from sqlalchemy import text

from tests.conftest import Editor, prepare, rect, region_op


def ready(client, pages=1):
    detail = prepare(client, pages=pages)
    return detail["drawing"]["id"], [p["id"] for p in detail["pages"]]


def test_first_edit_moves_to_in_progress_and_empty_transcription_autosaves(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    assert editor.acquire().status_code == 200
    response = editor.mutate([region_op(page, transcription="")])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["annotation_status"] == "IN_PROGRESS"
    assert (body["previous_annotation_version"], body["annotation_version"]) == ("0", "1")
    assert body["changed"]["regions"][0]["transcription"] == ""
    doc = client.get(f"/api/annotation/drawings/{drawing_id}/document").json()
    assert doc["annotation_version"] == "1" and len(doc["regions"]) == 1


def test_leases_views_and_navigation_do_not_change_version(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    client.put(f"/api/annotation/drawings/{drawing_id}/browser-progress", json={
        "browser_id": editor.browser_id, "page_id": page, "center_x": 10, "center_y": 20,
        "zoom_relative_to_fit": 2.5, "selected_region_id": str(uuid.uuid4())})
    detail = client.get(f"/api/annotation/drawings/{drawing_id}").json()
    assert detail["drawing"]["annotation_version"] == "0" and detail["drawing"]["annotation_status"] == "TO_DO"
    progress = client.get(f"/api/annotation/drawings/{drawing_id}/browser-progress",
                          params={"browser_id": editor.browser_id}).json()["progress"]
    assert progress["zoom_relative_to_fit"] == 2.5 and progress["selected_region_id"] is None


def test_nominal_and_tolerances_form_one_group_and_shared_note_supports_two(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    nominal, upper, lower, note, title = (str(uuid.uuid4()) for _ in range(5))
    g1, g2, g3 = (str(uuid.uuid4()) for _ in range(3))
    ops = [
        region_op(page, rect(10, 10, 20, 10), id=nominal, transcription="20"),
        region_op(page, rect(32, 8, 20, 5), id=upper, transcription="+0.25"),
        region_op(page, rect(32, 14, 20, 5), id=lower, transcription="0"),
        region_op(page, rect(10, 100, 200, 10), id=note, transcription="CASE HARDEN 58-62 HRC DEPTH 0.8-1.2"),
        region_op(page, rect(300, 500, 100, 20), id=title, transcription="DRG-1234 REV B",
                  semantic_disposition="NON_REQUIREMENT"),
        {"type": "group.create", "group": {"id": g1, "primary_page_id": page, "requirement_type": "DIMENSION",
                                           "structured_value": {"nominal": "20", "upper_tolerance": "+0.25",
                                                                "lower_tolerance": "0", "unit": "mm"}}},
        {"type": "member.add", "group_id": g1, "region_id": nominal, "role": "NOMINAL", "order_index": 0},
        {"type": "member.add", "group_id": g1, "region_id": upper, "role": "UPPER_TOLERANCE", "order_index": 1},
        {"type": "member.add", "group_id": g1, "region_id": lower, "role": "LOWER_TOLERANCE", "order_index": 2},
        {"type": "group.create", "group": {"id": g2, "primary_page_id": page, "requirement_type": "HEAT_TREATMENT"}},
        {"type": "member.add", "group_id": g2, "region_id": note, "role": "PRIMARY"},
        {"type": "group.create", "group": {"id": g3, "primary_page_id": page, "requirement_type": "HEAT_TREATMENT"}},
        {"type": "member.add", "group_id": g3, "region_id": note, "role": "PRIMARY"},
    ]
    response = editor.mutate(ops)
    assert response.status_code == 200, response.text
    numbers = {g["id"]: g["display_number"] for g in response.json()["changed"]["groups"]}
    assert sorted(numbers.values()) == [1, 2, 3]
    # Deleting a group keeps its source regions; numbers are never reused.
    response = editor.mutate([{"type": "group.delete", "group_id": g3}])
    assert response.json()["deleted"]["group_ids"] == [g3]
    g4 = str(uuid.uuid4())
    response = editor.mutate([{"type": "group.create", "group": {"id": g4, "primary_page_id": page,
                                                                 "requirement_type": "OTHER"}}])
    assert response.json()["changed"]["groups"][0]["display_number"] == 4
    doc = client.get(f"/api/annotation/drawings/{drawing_id}/document").json()
    assert len(doc["regions"]) == 5


def test_invalid_final_operation_rolls_back_entire_batch(client, engine):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    group = str(uuid.uuid4())
    response = editor.mutate([
        region_op(page),
        {"type": "group.create", "group": {"id": group, "primary_page_id": page, "requirement_type": "DIMENSION"}},
        region_op(page, [[0, 0], [10, 10], [10, 0], [0, 10]]),  # self-intersecting
    ])
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_GEOMETRY"
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.regions")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM annotation.mutation_receipts")).scalar() == 0
        row = conn.execute(text("SELECT annotation_version, next_requirement_number, annotation_status "
                                "FROM annotation.drawings")).one()
    assert tuple(row) == (0, 1, "TO_DO")


def test_region_delete_requires_removing_references_in_same_batch(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    region, group = str(uuid.uuid4()), str(uuid.uuid4())
    editor.mutate([region_op(page, id=region, transcription="Ø20"),
                   {"type": "group.create", "group": {"id": group, "primary_page_id": page,
                                                      "requirement_type": "DIMENSION"}},
                   {"type": "member.add", "group_id": group, "region_id": region, "role": "PRIMARY"}])
    response = editor.mutate([{"type": "region.delete", "region_id": region}])
    assert response.status_code == 422 and response.json()["error"]["code"] == "REGION_HAS_REFERENCES"
    response = editor.mutate([{"type": "member.remove", "group_id": group, "region_id": region, "role": "PRIMARY"},
                              {"type": "region.delete", "region_id": region}])
    assert response.status_code == 200, response.text


def test_membership_rules(client):
    drawing_id, (p1, p2) = ready(client, pages=2)
    editor = Editor(client, drawing_id)
    editor.acquire()
    a, b, ignore, group = (str(uuid.uuid4()) for _ in range(4))
    assert editor.mutate([
        region_op(p1, id=a, transcription="12", semantic_disposition="NON_REQUIREMENT"),
        region_op(p2, id=b, transcription="GENERAL NOTE"),
        region_op(p1, rect(100, 100, 10, 10), id=ignore, kind="IGNORE", legibility="UNREADABLE"),
        {"type": "group.create", "group": {"id": group, "primary_page_id": p1, "requirement_type": "DIMENSION"}},
        {"type": "member.add", "group_id": group, "region_id": a, "role": "PRIMARY"},
        {"type": "member.add", "group_id": group, "region_id": b, "role": "CONTEXT"},  # other page is fine for CONTEXT
    ]).status_code == 200
    doc = client.get(f"/api/annotation/drawings/{drawing_id}/document").json()
    assert next(r for r in doc["regions"] if r["id"] == a)["semantic_disposition"] == "UNCLASSIFIED"
    bad = editor.mutate([{"type": "member.add", "group_id": group, "region_id": b, "role": "PRIMARY"}])
    assert bad.json()["error"]["code"] == "INVALID_MEMBERSHIP"
    bad = editor.mutate([{"type": "member.add", "group_id": group, "region_id": ignore, "role": "SYMBOL"}])
    assert bad.json()["error"]["code"] == "INVALID_MEMBERSHIP"
    bad = editor.mutate([{"type": "region.update", "region_id": a, "changes": {"semantic_disposition": "NON_REQUIREMENT"}}])
    assert bad.status_code == 422


def test_strict_operation_fields(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    region = str(uuid.uuid4())
    editor.mutate([region_op(page, id=region)])
    for op in ({"type": "region.update", "region_id": region, "changes": {"origin": "OCR_ACCEPTED"}},
               {"type": "region.update", "region_id": region, "changes": {"annotation_status": "COMPLETED"}},
               {"type": "drawing.update", "changes": {}}):
        assert editor.mutate([op]).status_code == 422
    assert editor.mutate([]).status_code == 422  # empty batch
    too_many = [region_op(page, rect(1 + i, 1, 0.5, 0.5)) for i in range(201)]
    assert editor.mutate(too_many).status_code in (413, 422)


def test_lost_response_retry_returns_prior_result_and_changed_payload_is_rejected(client, engine):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    mutation_id = str(uuid.uuid4())
    ops = [region_op(page, transcription="+0.25")]
    first = editor.mutate(ops, mutation_id=mutation_id, expected="0")
    retry = editor.mutate(ops, mutation_id=mutation_id, expected="0")
    assert retry.status_code == 200 and retry.json()["annotation_version"] == first.json()["annotation_version"]
    assert retry.json()["replayed"] is True
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.regions")).scalar() == 1
    changed = editor.mutate([region_op(page, transcription="+0.30")], mutation_id=mutation_id, expected="0")
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    receipt = client.get(f"/api/annotation/drawings/{drawing_id}/mutations/{mutation_id}").json()
    assert receipt["resulting_version"] == "1" and "lease_token" not in str(receipt)
    assert client.get(f"/api/annotation/drawings/{drawing_id}/mutations/{uuid.uuid4()}").status_code == 404


def test_retry_after_reacquiring_lease_resolves_by_receipt(client, engine):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    mutation_id = str(uuid.uuid4())
    ops = [region_op(page)]
    editor.mutate(ops, mutation_id=mutation_id, expected="0")
    with engine.begin() as conn:
        conn.execute(text("UPDATE annotation.editing_leases SET expires_at = now() - interval '1 second'"))
    # Old token, expired lease: the receipt still resolves the committed write.
    retry = editor.mutate(ops, mutation_id=mutation_id, expected="0")
    assert retry.status_code == 200 and retry.json()["replayed"] is True


def test_version_conflict(client):
    drawing_id, (page,) = ready(client)
    editor = Editor(client, drawing_id)
    editor.acquire()
    editor.mutate([region_op(page)])
    response = editor.mutate([region_op(page)], expected="0")
    assert response.status_code == 409
    assert response.json()["error"] == {"code": "VERSION_CONFLICT", "current_annotation_version": "1",
                                        "message": "This drawing changed before your edit was saved.", "details": []}


def test_two_browsers_race_for_lease_and_stale_tokens_cannot_write_or_release(client, engine):
    drawing_id, (page,) = ready(client)
    a, b = Editor(client, drawing_id), Editor(client, drawing_id)
    results = {}

    def go(name, editor):
        results[name] = editor.acquire().status_code

    threads = [threading.Thread(target=go, args=("a", a)), threading.Thread(target=go, args=("b", b))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == [200, 423]
    winner, loser = (a, b) if results["a"] == 200 else (b, a)
    held = loser.acquire()
    assert held.json()["error"]["code"] == "LEASE_HELD"
    assert held.json()["error"]["message"] == "Someone is editing this drawing. You can view it or try again shortly."
    assert "browser" not in str(client.get(f"/api/annotation/drawings/{drawing_id}").json()["lease"])
    loser.token, loser.generation = "forged-token-value-123", winner.generation
    assert loser.mutate([region_op(page)]).json()["error"]["code"] == "LEASE_LOST"
    # Winner's lease expires; loser takes over; the stale winner can neither write nor release it.
    with engine.begin() as conn:
        conn.execute(text("UPDATE annotation.editing_leases SET expires_at = now() - interval '1 second'"))
    assert loser.acquire().status_code == 200
    assert winner.mutate([region_op(page)]).status_code == 423
    stale_release = client.request("DELETE", f"/api/annotation/drawings/{drawing_id}/edit-session",
                                   json={"lease_token": winner.token, "lease_generation": winner.generation})
    assert stale_release.json() == {"released": False}
    assert loser.mutate([region_op(page)]).status_code == 200
    heartbeat = client.patch(f"/api/annotation/drawings/{drawing_id}/edit-session",
                             json={"lease_token": loser.token, "lease_generation": loser.generation})
    assert heartbeat.status_code == 200


def test_duplicate_tab_and_reload_cannot_take_over_live_lease(client):
    drawing_id, _ = ready(client)
    browser = str(uuid.uuid4())
    first = Editor(client, drawing_id, browser)
    assert first.acquire().status_code == 200
    duplicate = Editor(client, drawing_id, browser)  # same browser, new tab instance
    response = duplicate.acquire()
    assert response.status_code == 423 and response.json()["error"]["retry_after_seconds"] > 0
    client.request("DELETE", f"/api/annotation/drawings/{drawing_id}/edit-session",
                   json={"lease_token": first.token, "lease_generation": first.generation})
    assert duplicate.acquire().status_code == 200
    assert int(duplicate.generation) == int(first.generation) + 1


def test_edit_before_preparation_ready_is_rejected(client):
    from tests.conftest import make_pdf, upload
    drawing_id = upload(client, make_pdf()).json()["drawing"]["id"]
    editor = Editor(client, drawing_id)
    assert editor.acquire().json()["error"]["code"] == "PREPARATION_NOT_READY"


def test_library_order_not_moved_by_views(client):
    first_id, _ = ready(client)
    time.sleep(0.01)
    second_id, (page2,) = ready(client)
    listing = client.get("/api/annotation/drawings").json()["items"]
    assert [i["id"] for i in listing] == [second_id, first_id]
    editor = Editor(client, first_id)
    editor.acquire()  # leases and views do not reorder
    assert [i["id"] for i in client.get("/api/annotation/drawings").json()["items"]] == [second_id, first_id]
    listing = client.get("/api/annotation/drawings").json()["items"]
    assert next(i for i in listing if i["id"] == first_id)["being_edited"] is True
