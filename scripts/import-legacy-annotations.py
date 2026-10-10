#!/usr/bin/env python3
"""Explicit, repeatable import of the legacy ingestor prototype (SQLite + data/ files) into PostgreSQL.

Dry run is the default: it only inventories and reports. Nothing is imported without --apply, and --apply
requires --backup-dir, where a consistent copy of the SQLite database and every referenced source file is
written first. The legacy database and data/ directory are only ever read.

Policy (see the implementation plan, section 17):
- Originals become SOURCE assets with their exact byte hashes; duplicates are resolved by source hash.
- Legacy renders are NOT reused: new renders are produced by the preparation worker, so every page frame
  is known exactly. Legacy geometry is imported only when the legacy sheet frame equals the new page frame.
- Machine OCR observations and automatic/model requirements are never imported as human labels. The optional
  suggestions layer is not enabled in this version, so they are reported, not imported.
- Only clearly human-attributable requirements are imported, as draft LEGACY_IMPORT regions and groups.
  Their normalized legacy text is not a verified transcription, so regions get an empty transcription and
  the legacy text is kept as the requirement description for the engineer to verify.
- Imported drawings stay TO_DO (no human work) or become IN_PROGRESS (human work imported); never COMPLETED.
- Reruns add nothing twice (annotation.legacy_import_map records every imported legacy ID).

Usage:
  .venv/bin/python scripts/import-legacy-annotations.py --sqlite ../ingestor/data/ingestor.sqlite3
  .venv/bin/python scripts/import-legacy-annotations.py --sqlite ... --apply --backup-dir /path/to/backup
Run it again after the worker has prepared the imported drawings to import human geometry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from apps.api.app.annotation.database import transaction  # noqa: E402
from apps.api.app.annotation.errors import ApiError  # noqa: E402
from apps.api.app.annotation.services.drawings import register_upload  # noqa: E402
from packages.annotation_domain.geometry import GeometryError, normalize_quad  # noqa: E402

MACHINE_ORIGINS = {"automatic", "machine", "vlm", "model", "parser", "ocr"}
TYPE_MAP = {"dimension": "DIMENSION", "gdt": "GEOMETRIC_TOLERANCE", "geometric_tolerance": "GEOMETRIC_TOLERANCE",
            "surface_finish": "SURFACE_FINISH", "thread": "THREAD", "material": "MATERIAL",
            "heat_treatment": "HEAT_TREATMENT", "coating": "COATING", "note": "MANUFACTURING_NOTE"}
FRAME_TOLERANCE = 0.01


@dataclass
class Report:
    revisions: int = 0
    sources_missing: list[str] = field(default_factory=list)
    sources_hash_mismatch: list[str] = field(default_factory=list)
    unsupported_transforms: list[str] = field(default_factory=list)
    machine_observations: int = 0
    machine_requirements: int = 0
    human_requirements: int = 0
    drawings_created: int = 0
    drawings_existing: int = 0
    human_imported: int = 0
    human_skipped: list[str] = field(default_factory=list)
    waiting_for_preparation: list[str] = field(default_factory=list)

    def print(self, applied: bool) -> None:
        print(json.dumps({"mode": "apply" if applied else "dry-run", **self.__dict__}, indent=2, default=str))


def connect_legacy(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only: the legacy DB is never modified
    conn.row_factory = sqlite3.Row
    return conn


def is_human(legacy: sqlite3.Connection, requirement: sqlite3.Row) -> bool:
    if (requirement["origin"] or "").lower() not in MACHINE_ORIGINS:
        return True
    actors = {r[0] for r in legacy.execute("SELECT DISTINCT actor FROM requirement_history WHERE requirement_id = ?",
                                           (requirement["id"],))}
    # An automatic proposal that a person later edited is human work only if a non-system actor changed it.
    return bool(actors - {"system", "", None}) and requirement["review_state"] not in ("rejected",)


def backup(legacy_path: Path, legacy: sqlite3.Connection, backup_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"legacy-backup-{stamp}"
    (target / "sources").mkdir(parents=True)
    with sqlite3.connect(target / legacy_path.name) as copy:
        legacy.backup(copy)
    for row in legacy.execute("SELECT source_path FROM revisions"):
        source = Path(row[0])
        if source.is_file():
            shutil.copy2(source, target / "sources" / source.name)
    print(f"Backup written to {target}", file=sys.stderr)
    return target


def mapped(conn, kind: str, legacy_id: str) -> str | None:
    value = conn.execute(text("SELECT entity_id FROM annotation.legacy_import_map WHERE legacy_kind = :k AND legacy_id = :i"),
                         {"k": kind, "i": legacy_id}).scalar()
    return str(value) if value else None


def record(conn, kind: str, legacy_id: str, entity_id: str, drawing_id: str, details: dict | None = None) -> None:
    conn.execute(text(
        "INSERT INTO annotation.legacy_import_map (legacy_kind, legacy_id, entity_id, drawing_id, details) "
        "VALUES (:k, :i, :e, :d, CAST(:details AS jsonb)) ON CONFLICT DO NOTHING"),
        {"k": kind, "i": legacy_id, "e": entity_id, "d": drawing_id, "details": json.dumps(details or {})})


def import_revision(legacy: sqlite3.Connection, revision: sqlite3.Row, report: Report, apply: bool) -> None:
    source = Path(revision["source_path"])
    if not source.is_file():
        report.sources_missing.append(f"{revision['id']}: {source}")
        return
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != revision["source_sha256"]:
        report.sources_hash_mismatch.append(revision["id"])
        return
    sheets = legacy.execute("SELECT * FROM sheets WHERE revision_id = ? ORDER BY ordinal", (revision["id"],)).fetchall()
    for sheet in sheets:
        if int(sheet["rotation"]) % 90:
            report.unsupported_transforms.append(f"{sheet['id']}: rotation {sheet['rotation']}")
    requirements = legacy.execute("SELECT * FROM requirements WHERE revision_id = ?", (revision["id"],)).fetchall()
    human = [r for r in requirements if is_human(legacy, r)]
    report.machine_requirements += len(requirements) - len(human)
    report.human_requirements += len(human)
    report.machine_observations += legacy.execute("SELECT count(*) FROM observations WHERE revision_id = ?",
                                                  (revision["id"],)).fetchone()[0]
    if not apply:
        return
    with transaction() as conn:
        drawing_id = mapped(conn, "REVISION", revision["id"])
    if drawing_id is None:
        try:
            drawing, duplicate = register_upload(revision["filename"], data)
        except ApiError as exc:
            report.human_skipped.append(f"{revision['id']}: source rejected ({exc.code})")
            return
        drawing_id = str(drawing["id"])
        report.drawings_existing += int(duplicate)
        report.drawings_created += int(not duplicate)
        with transaction() as conn:
            record(conn, "REVISION", revision["id"], drawing_id, drawing_id,
                   {"legacy_filename": revision["filename"], "legacy_sha256": revision["source_sha256"]})
    else:
        report.drawings_existing += 1
    if human:
        import_human(drawing_id, sheets, human, report)


def import_human(drawing_id: str, sheets: list[sqlite3.Row], human: list[sqlite3.Row], report: Report) -> None:
    with transaction() as conn:
        drawing = conn.execute(text(
            "SELECT preparation_state, annotation_status, annotation_version, next_requirement_number "
            "FROM annotation.drawings WHERE id = :id FOR UPDATE"), {"id": drawing_id}).mappings().one()
        if drawing["preparation_state"] != "READY":
            report.waiting_for_preparation.append(drawing_id)
            return
        if drawing["annotation_status"] == "COMPLETED":
            report.human_skipped.append(f"{drawing_id}: drawing is completed; reopen it before importing")
            return
        active = conn.execute(text("SELECT 1 FROM annotation.editing_leases WHERE drawing_id = :id AND expires_at > now() "
                                   "AND token_hash <> '' FOR UPDATE"), {"id": drawing_id}).first()
        if active:
            report.human_skipped.append(f"{drawing_id}: someone is editing; rerun later")
            return
        pages = {r["page_number"]: dict(r) for r in conn.execute(text(
            "SELECT id, page_number, width, height FROM annotation.pages WHERE drawing_id = :id"), {"id": drawing_id}).mappings()}
        sheet_by_id = {s["id"]: s for s in sheets}
        number = int(drawing["next_requirement_number"])
        imported = 0
        for requirement in human:
            if mapped(conn, "REQUIREMENT", requirement["id"]):
                continue
            sheet = sheet_by_id.get(requirement["sheet_id"]) or (sheets[0] if len(sheets) == 1 else None)
            page = pages.get(int(sheet["ordinal"])) if sheet else None
            if page is None:
                report.human_skipped.append(f"{requirement['id']}: unknown sheet")
                continue
            if abs(float(sheet["width_pt"]) - page["width"]) > FRAME_TOLERANCE or abs(float(sheet["height_pt"]) - page["height"]) > FRAME_TOLERANCE:
                report.human_skipped.append(f"{requirement['id']}: legacy frame differs from the new page frame; not guessed")
                continue
            region_ids = []
            for index, polygon in enumerate(json.loads(requirement["evidence_json"] or "[]")):
                try:
                    quad = normalize_quad(_to_quad(polygon), page["width"], page["height"])
                except (GeometryError, ValueError):
                    report.human_skipped.append(f"{requirement['id']}: evidence {index} has invalid geometry")
                    continue
                region_id = str(uuid.uuid4())
                conn.execute(text(
                    "INSERT INTO annotation.regions (id, drawing_id, page_id, kind, polygon, transcription, origin) "
                    "VALUES (:id, :d, :p, 'TEXT', CAST(:poly AS jsonb), '', 'LEGACY_IMPORT')"),
                    {"id": region_id, "d": drawing_id, "p": str(page["id"]), "poly": json.dumps(quad)})
                record(conn, "EVIDENCE", f"{requirement['id']}#{index}", region_id, drawing_id)
                region_ids.append(region_id)
            if not region_ids:
                continue
            group_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO annotation.requirement_groups (id, drawing_id, primary_page_id, display_number, "
                "requirement_type, description) VALUES (:id, :d, :p, :n, :t, :desc)"),
                {"id": group_id, "d": drawing_id, "p": str(page["id"]), "n": number,
                 "t": TYPE_MAP.get((requirement["kind"] or "").lower(), "OTHER"),
                 "desc": f"Legacy text (verify against the drawing): {requirement['raw_text']}"[:2000]})
            for order, region_id in enumerate(region_ids):
                conn.execute(text(
                    "INSERT INTO annotation.group_members (drawing_id, group_id, region_id, role, order_index) "
                    "VALUES (:d, :g, :r, 'PRIMARY', :o)"), {"d": drawing_id, "g": group_id, "r": region_id, "o": order})
            record(conn, "REQUIREMENT", requirement["id"], group_id, drawing_id, {"legacy_origin": requirement["origin"]})
            number += 1
            imported += 1
        if imported:
            conn.execute(text(
                "UPDATE annotation.drawings SET annotation_status = 'IN_PROGRESS', annotation_version = annotation_version + 1, "
                "next_requirement_number = :n, annotation_updated_at = now() WHERE id = :id"), {"n": number, "id": drawing_id})
        report.human_imported += imported


def _to_quad(polygon: list) -> list[list[float]]:
    points = [[float(x), float(y)] for x, y in polygon]
    if len(points) == 4:
        return points
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    raise ValueError(f"expected 4 points, got {len(points)} ({min(xs)},{min(ys)})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", type=Path, required=True, help="legacy ingestor.sqlite3 (opened read-only)")
    parser.add_argument("--apply", action="store_true", help="perform the import (default is a dry run)")
    parser.add_argument("--backup-dir", type=Path, help="required with --apply; receives a copy of the legacy data")
    args = parser.parse_args()
    if not args.sqlite.is_file():
        parser.error(f"{args.sqlite} does not exist")
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    legacy = connect_legacy(args.sqlite)
    if args.apply:
        backup(args.sqlite, legacy, args.backup_dir)
    report = Report()
    for revision in legacy.execute("SELECT * FROM revisions ORDER BY created_at").fetchall():
        report.revisions += 1
        import_revision(legacy, revision, report, args.apply)
    report.print(args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
