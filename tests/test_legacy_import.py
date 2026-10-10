"""Legacy import: dry run by default, backup before apply, human-only labels, repeatable."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from tests.conftest import make_pdf
from workers.annotation_worker.main import run_once

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import-legacy-annotations.py"


def legacy_db(tmp_path: Path) -> Path:
    source = tmp_path / "source.pdf"
    source.write_bytes(make_pdf(size=(600, 400)))
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    db = tmp_path / "ingestor.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE revisions (id TEXT, filename TEXT, source_sha256 TEXT, source_path TEXT, created_at TEXT);
    CREATE TABLE sheets (id TEXT, revision_id TEXT, ordinal INTEGER, width_pt REAL, height_pt REAL, rotation INTEGER, user_unit REAL);
    CREATE TABLE requirements (id TEXT, revision_id TEXT, origin TEXT, review_state TEXT, kind TEXT, raw_text TEXT,
                               evidence_json TEXT, sheet_id TEXT);
    CREATE TABLE requirement_history (requirement_id TEXT, actor TEXT);
    CREATE TABLE observations (id TEXT, revision_id TEXT);
    """)
    conn.execute("INSERT INTO revisions VALUES ('rev1', 'legacy.pdf', ?, ?, '2026-01-01')", (digest, str(source)))
    conn.execute("INSERT INTO sheets VALUES ('sheet1', 'rev1', 1, 600, 400, 0, 1.0)")
    quad = [[10, 10], [60, 10], [60, 30], [10, 30]]
    conn.execute("INSERT INTO requirements VALUES ('human1', 'rev1', 'manual', 'accepted', 'dimension', 'diameter 20', ?, 'sheet1')",
                 (json.dumps([quad]),))
    conn.execute("INSERT INTO requirements VALUES ('auto1', 'rev1', 'automatic', 'unreviewed', 'dimension', '12', ?, 'sheet1')",
                 (json.dumps([quad]),))
    conn.execute("INSERT INTO requirement_history VALUES ('auto1', 'system')")
    conn.executemany("INSERT INTO observations VALUES (?, 'rev1')", [(f"o{i}",) for i in range(5)])
    conn.commit()
    conn.close()
    return db


def run(*args):
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_dry_run_changes_nothing(tmp_path, engine):
    db = legacy_db(tmp_path)
    before = db.read_bytes()
    report = run("--sqlite", str(db))
    assert report["mode"] == "dry-run" and report["human_requirements"] == 1 and report["machine_requirements"] == 1
    assert report["machine_observations"] == 5
    assert db.read_bytes() == before
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM annotation.drawings")).scalar() == 0


def test_apply_is_repeatable_and_imports_only_human_work(tmp_path, engine):
    db = legacy_db(tmp_path)
    first = run("--sqlite", str(db), "--apply", "--backup-dir", str(tmp_path / "backup"))
    assert first["drawings_created"] == 1 and first["waiting_for_preparation"]
    assert list((tmp_path / "backup").glob("legacy-backup-*/ingestor.sqlite3"))
    while run_once():
        pass
    second = run("--sqlite", str(db), "--apply", "--backup-dir", str(tmp_path / "backup"))
    assert second["human_imported"] == 1 and second["drawings_created"] == 0
    third = run("--sqlite", str(db), "--apply", "--backup-dir", str(tmp_path / "backup"))
    assert third["human_imported"] == 0
    with engine.begin() as conn:
        status = conn.execute(text("SELECT annotation_status FROM annotation.drawings")).scalar()
        regions = conn.execute(text("SELECT origin, transcription FROM annotation.regions")).all()
        groups = conn.execute(text("SELECT description FROM annotation.requirement_groups")).all()
    assert status == "IN_PROGRESS"
    assert [tuple(r) for r in regions] == [("LEGACY_IMPORT", "")]  # legacy normalized text is not a transcription
    assert "diameter 20" in groups[0][0]
