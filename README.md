# Mechanical drawing annotation workspace

A shared web workspace where mechanical engineers upload drawings, label every text line and engineering
symbol, group manufacturing requirements, leave and resume at any time, and finish drawings so they become
available to a future training pipeline. This implements
`../ingestor/annotation-workspace-implementation-plan.md` as a new application. It adapts the ingestor
prototype's viewer and geometry code, and it does not change the prototype or its data.

- **Two screens.** The shared drawing library (`/#/drawings`) and the annotation workspace (`/#/drawings/{id}`).
- **Three workflow statuses.** `TO DO`, `IN PROGRESS` and `COMPLETED`. Preparing, preparation failures and "being edited" are shown as secondary messages, not extra statuses.
- **No accounts.** There is no login, reviewer role, approval queue, export screen or training launcher.
- **Everything lives in PostgreSQL.** Original files, page renders, thumbnails, labels, progress, receipts and training snapshots are all stored in the database (`BYTEA` for binaries). Worker files are temporary.

> **Access model.** Anyone who can reach the application can read every drawing and make the allowed
> edits. Browser IDs and editing leases prevent accidental collisions. They are not access control. Put the
> deployment behind your network or VPN controls.

## Layout

```text
apps/api/app/annotation_main.py        FastAPI entrypoint (the only served app)
apps/api/app/annotation/               router, schemas, config, database, repository, validation, snapshot
apps/api/app/annotation/services/      drawings, uploads, assets, leases, mutations, receipts, completion, progress
workers/annotation_worker/             durable preparation worker (main.py) and sandboxed renderer (rendering.py)
packages/annotation_domain/            shared vocabulary.json, canonical JSON, page-frame geometry
packages/annotation_training/          internal training reader and crop/label adapters (no HTTP surface)
migrations/annotation/                 Alembic migrations for the `annotation` schema
apps/review/src/                       React + TypeScript frontend (library, workspace, autosave engine)
scripts/                               start scripts, migrate, smoke check, legacy import
tests/                                 backend integration tests (real PostgreSQL)
e2e/                                   Playwright browser tests (isolated stack)
deploy/, Dockerfile, docker-compose.yml, render.yaml  one-image container (start.sh) and Render Blueprint
```

## Run locally (recommended path)

Requirements: Python 3.12, Node 22 with pnpm, and Docker (for PostgreSQL 18).

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
pnpm install
./scripts/start-db.sh          # PostgreSQL 18 on 127.0.0.1:55432
./scripts/migrate.sh           # alembic upgrade head (a deployment step, never run at API startup)
./scripts/start-api.sh         # API on 127.0.0.1:8010
./scripts/start-worker.sh      # preparation worker (separate process)
./scripts/start-frontend.sh    # Vite on http://127.0.0.1:5174 (proxies /api to 8010)
```

Open http://127.0.0.1:5174. No model server, GPU or model account is needed. OCR suggestions are optional and
need the `tesseract` binary (see [OCR suggestions](#ocr-suggestions)).

To run everything in containers (Postgres plus one app image that migrates, then runs the preparation worker and the API, which serves the frontend; OCR is on):

```sh
docker compose up --build      # http://127.0.0.1:8080
```

The same image runs on Render's free tier: see [deploy/RENDER.md](deploy/RENDER.md) and `render.yaml`.

## How the workflow maps to the implementation

| Behavior | Where |
| --- | --- |
| Upload PDF, PNG or JPEG; checks file signature and parser; rejects encrypted and malformed files; exact duplicates return the existing drawing | `services/uploads.py`, `services/drawings.py` |
| Durable render jobs: `FOR UPDATE SKIP LOCKED` claims, lease plus generation, heartbeats, stale-worker fencing, retry with backoff, explicit retry that keeps published page frames | `workers/annotation_worker/main.py` |
| Canonical page frames (PDF CropBox, `/Rotate`, UserUnit; image EXIF orientation) and a stored canonical-to-render matrix | `workers/annotation_worker/rendering.py`, `packages/annotation_domain/geometry.py` |
| Atomic mutation batches with strict operations; the whole final state is validated and written in foreign-key-safe order | `services/mutations.py`, `validation.py` |
| Idempotency: a receipt lookup comes before every other check; a reused ID with a different payload is rejected | `services/receipts.py` |
| Editing leases (90 s, 20 s heartbeat), no forced takeover; a stale release cannot end a newer lease | `services/leases.py`, `hooks/useEditLease.ts` |
| Autosave: at most one batch in flight; IndexedDB outbox written before sending; typing coalesced (600 ms debounce, 2 s max wait); unknown outcomes resolved by receipt | `state/editorStore.ts`, `state/annotationReducer.ts`, `lib/localDrafts.ts` |
| Reload recovery, conflict recovery (keep saved version or apply my change for each item), deferred edits to completed drawings | `lib/recovery.ts`, `components/DraftRecovery.tsx`, `pages/AnnotationPage.tsx` |
| Per-browser page and view restore (center plus zoom relative to fit) | `hooks/useBrowserProgress.ts`, `services/progress.py` |
| Finish: flush, validate, create an immutable snapshot, set COMPLETED, invalidate the lease, write a receipt, all in one transaction | `services/completion.py`, `snapshot.py` |
| Reopen: clears the active snapshot pointer; historical snapshots are kept | `services/completion.py` |
| Training interface: `ready_training_snapshots` view, checksum-verified reader, detection, recognition and requirement adapters | `packages/annotation_training/` |

### Labeling policy (`mechanical-labeling-v1`)

- Completed pages are **fully labeled**: every visible text line or token and every relevant engineering symbol, including title-block metadata. Metadata gets OCR labels but no requirement number. The in-app instructions say this.
- Transcriptions keep the visible characters (`Ø20`, `±`, `°`, trailing zeros). Symbols use the versioned codes in `packages/annotation_domain/vocabulary.json`. Glyphs with no Unicode form are class-only targets and no character is invented for them.
- **Unreadable** regions carry no transcription and are ignore areas for detection. They are excluded from recognition.
- Requirement groups are the positive requirement labels. Only regions explicitly marked **Not a requirement** are negatives. Unclassified text and CONTEXT-only links are never used as negatives.
- Requirement numbers are allocated by the server under the drawing lock. They are unique and increasing, and deleted numbers are never reused.

### Training reader

```python
from apps.api.app.annotation.database import get_engine
from packages.annotation_training.reader import list_ready_snapshot_ids, load_snapshot, load_asset_bytes
from packages.annotation_training.adapters import iter_detection_examples, iter_recognition_examples, iter_requirement_examples, crop_region

with get_engine().connect() as conn:          # use a read-only database role in production
    ids = list_ready_snapshot_ids(conn)       # pin these in your run manifest
    snapshot = load_snapshot(conn, ids[0])    # verifies the canonical SHA-256
    for example in iter_recognition_examples(snapshot):
        render = load_asset_bytes(conn, example.render_asset_id, example.render_sha256)
        crop = crop_region(render, example)   # oriented quad crop, never an axis-aligned box
```

Keep every page and crop from the same drawing in the same split, and group related drawings by
`lineage_key` before reporting held-out scores. This application does not detect near-duplicate families.

A suggested read-only role for training readers:

```sql
CREATE ROLE annotation_training_reader LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA annotation TO annotation_training_reader;
GRANT SELECT ON annotation.ready_training_snapshots, annotation.training_snapshots, annotation.assets TO annotation_training_reader;
```

`assets` and `training_snapshots` rows cannot be updated or deleted, whatever role connects (database triggers). Published page frames cannot be changed either.

## Configuration

See `.env.example` for every setting and its default. The main ones are:

- `DATABASE_URL`, `APP_PUBLIC_ORIGIN` (comma-separated; mutating requests from any other `Origin` are rejected), `TRUSTED_HOSTS`, `FRONTEND_DIST` (optional static serving)
- Upload, page, pixel and asset limits: 50 MiB, 100 pages, 64 MP, 40 MiB per render, 1 GiB per drawing
- `RENDER_DPI` (300), `RENDER_TIMEOUT_SECONDS` (120), `RENDER_MEMORY_LIMIT_MIB`
- `EDIT_LEASE_SECONDS` (90), `EDIT_HEARTBEAT_SECONDS` (20), worker poll, heartbeat, lease and attempt settings, `WORKER_CONCURRENCY`
- `ENABLE_OCR_SUGGESTIONS` (false), `OCR_TIMEOUT_SECONDS` (10), `OCR_MAX_CROP_PIXELS` (4,000,000), `OCR_CONCURRENCY` (2), `OCR_LANGUAGE` (eng). See [OCR suggestions](#ocr-suggestions).

These limits are initial operating limits to check against the plant's sample drawings. They are not measured capacity.

## OCR suggestions

Set `ENABLE_OCR_SUGGESTIONS=true` and install Tesseract (`brew install tesseract`, or `apt-get install tesseract-ocr`; the
Docker image already includes it). When someone draws a **Text region**, the inspector reads the box and fills the
recognized text straight into the transcription field. It is a normal, editable transcription: correct it if the read is
wrong. Text you typed before the read finished is never overwritten.

- `POST /api/annotation/drawings/{id}/ocr-suggestions` with `{page_id, polygon}` returns `{text, confidence}`. It is read-only: no lease, no lock, nothing stored.
- The recognized text is saved like any typed text, as an ordinary edit (`origin` stays `MANUAL`), so annotators must check each filled box against the drawing before finishing.
- The box is rectified from the page render (`packages/annotation_domain/crop.py`), then read with Tesseract, first restricted to dimension characters and then open. Common digit confusions (`Q`→`0`, `,`→`.`) are fixed inside numeric tokens only.
- A failed, slow or empty read shows no suggestion. Always compare against the drawing: it is a guess.

## Verification

```sh
./scripts/start-db.sh
.venv/bin/pytest -q                  # backend integration tests on real PostgreSQL (creates annotation_test)
pnpm test                            # frontend unit tests (reducer, autosave engine, geometry, doc ops)
pnpm run build                       # type-check and production build
pnpm exec playwright install chromium && pnpm e2e   # browser tests on an isolated stack (annotation_e2e, ports 8011 and 5175)
DATABASE_URL=... .venv/bin/python scripts/smoke-check.py --api http://127.0.0.1:8010
```

The smoke check runs upload → render → annotate → resume from another browser → complete → training reader
against a running stack. It adds one generated `smoke-check-*.pdf` drawing to the library.

## Migrating ingestor prototype data

```sh
.venv/bin/python scripts/import-legacy-annotations.py --sqlite ../ingestor/data/ingestor.sqlite3                     # dry run (default)
.venv/bin/python scripts/import-legacy-annotations.py --sqlite ../ingestor/data/ingestor.sqlite3 --apply --backup-dir ~/legacy-backups
# after the worker has prepared the imported drawings, run the --apply command again to import human geometry
```

The legacy SQLite database is opened read-only, and `--apply` first writes a backup of the database and its
source files. Originals are imported with their exact hashes and rendered again (old renders are not reused).
Machine OCR and automatic requirements are reported but never imported as human labels, and the suggestions
layer is not part of this version. Clearly human requirements are imported as draft `LEGACY_IMPORT` regions
and groups, with an empty transcription and the legacy text kept as the description to verify. Nothing is
marked COMPLETED. Reruns are idempotent (`annotation.legacy_import_map`).

The current prototype database holds 1 drawing, 269 automatic requirements, 1,647 OCR observations and
no human edits. A migration would therefore create one TO DO drawing.

## Operations

- **Backups** must cover the whole database, which holds the sources, renders, labels and snapshots. Turn on managed backups and point-in-time recovery, and test one restore to a separate database before going to production.
- **Logs** record drawing, job and mutation IDs, stages and error codes. They do not record drawing contents, transcriptions or lease tokens.
- **Watch** failed saves (`VERSION_CONFLICT` and `LEASE_LOST` rates), preparation queue age and failures, asset table growth and database capacity.
- **Asset responses** are `Cache-Control: private` with checksum ETags. The API limits how many binary reads it serves at once.

## Not included in this version

- Model-based suggestions, and OCR that runs in the background or provenance-tracks accepted text (`origin` stays `MANUAL`)
- Native CAD formats: export DWG, DXF or STEP to PDF first
- Deleting drawings, replacing sources, revision management, a history browser
- Simultaneous collaborative editing: one tab edits a drawing at a time
- Automatic balloon leader routing or ballooned PDF export
- Load-test results. The plan's targets (20 browsers, 10 editors, 3,000 regions on one page, 1,000 drawings) still need to be measured on the chosen deployment.
