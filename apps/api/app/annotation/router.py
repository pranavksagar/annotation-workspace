"""HTTP routes mounted at /api/annotation. Read endpoints are side-effect free."""
from __future__ import annotations

import json
import threading
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from packages.annotation_domain.vocabulary import vocabulary

from .config import settings
from .database import schema_ready, snapshot_read
from .errors import ApiError, not_found
from .schemas import (BrowserProgressIn, CompleteRequest, EditSessionRequest, LeaseCredentials, MutationEnvelope,
                      OcrRequest, ReopenRequest)
from .services import completion, drawings, leases, mutations, ocr, progress
from .services.assets import asset_bytes, asset_meta

router = APIRouter(prefix="/api/annotation")
_asset_reads = threading.BoundedSemaphore(settings.max_concurrent_asset_reads)


def _validation_error(exc: ValidationError) -> ApiError:
    details = [{"code": "INVALID_FIELD", "message": e["msg"], "field": ".".join(str(p) for p in e["loc"])}
               for e in exc.errors(include_url=False)[:50]]
    return ApiError(422, "INVALID_REQUEST", "The request is not valid.", details=details)


async def _json_body(request: Request, limit: int) -> Any:
    body = await request.body()
    if len(body) > limit:
        raise ApiError(413, "BODY_TOO_LARGE", "This save is too large. Select fewer items and try again.")
    try:
        return json.loads(body or b"null")
    except ValueError as exc:
        raise ApiError(422, "INVALID_REQUEST", "The request body is not valid JSON.") from exc


def _parse(model, data: Any):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready() -> JSONResponse:
    try:
        ok = schema_ready()
    except Exception:
        ok = False
    return JSONResponse({"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503)


@router.get("/vocabulary")
def get_vocabulary() -> dict[str, Any]:
    return vocabulary()


@router.get("/drawings")
def list_drawings(q: str | None = Query(None, max_length=200), status: str | None = None,
                  cursor: str | None = Query(None, max_length=500), limit: int | None = None) -> dict[str, Any]:
    return drawings.list_drawings(q, status, cursor, limit or settings.library_page_size)


@router.post("/drawings")
async def upload_drawing(file: UploadFile = File(...)) -> JSONResponse:
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise ApiError(413, "UPLOAD_TOO_LARGE",
                           f"Files larger than {settings.max_upload_bytes // (1024 * 1024)} MiB are not accepted.")
        chunks.append(chunk)
    drawing, duplicate = await run_in_threadpool(drawings.register_upload, file.filename, b"".join(chunks))
    from .repository import drawing_json
    body = {"drawing": drawing_json(drawing), "duplicate": duplicate}
    if duplicate:
        body["message"] = "This drawing is already in the library"
    return JSONResponse(body, status_code=200 if duplicate else 201)


@router.get("/drawings/{drawing_id}")
def get_drawing(drawing_id: UUID) -> dict[str, Any]:
    return drawings.drawing_detail(str(drawing_id))


@router.get("/drawings/{drawing_id}/document")
def get_document(drawing_id: UUID) -> dict[str, Any]:
    return drawings.read_document(str(drawing_id))


@router.post("/drawings/{drawing_id}/retry-preparation")
def retry_preparation(drawing_id: UUID) -> dict[str, Any]:
    return drawings.retry_preparation(str(drawing_id))


@router.post("/drawings/{drawing_id}/ocr-suggestions")
def ocr_suggestion(drawing_id: UUID, body: OcrRequest) -> dict[str, Any]:
    """Read-only text suggestion for a box. Needs no lease and writes nothing."""
    return ocr.suggest(str(drawing_id), str(body.page_id), body.polygon)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: UUID, request: Request) -> Response:
    with snapshot_read() as conn:
        meta = asset_meta(conn, str(asset_id))
    if meta is None:
        raise not_found("Asset")
    etag = f'"{meta["sha256"].strip()}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable",
               "X-Content-Type-Options": "nosniff"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    # Bound how many large BYTEA values are held in API memory at once.
    with _asset_reads:
        with snapshot_read() as conn:
            data = asset_bytes(conn, str(asset_id))
    if data is None:
        raise not_found("Asset")
    return Response(content=data, media_type=meta["mime_type"], headers=headers)


@router.post("/drawings/{drawing_id}/edit-session")
def acquire_edit_session(drawing_id: UUID, body: EditSessionRequest) -> dict[str, Any]:
    return leases.acquire(str(drawing_id), str(body.browser_id), str(body.tab_id))


@router.patch("/drawings/{drawing_id}/edit-session")
def heartbeat_edit_session(drawing_id: UUID, body: LeaseCredentials) -> dict[str, Any]:
    return leases.heartbeat(str(drawing_id), body.lease_token, body.lease_generation)


@router.delete("/drawings/{drawing_id}/edit-session")
async def release_edit_session(drawing_id: UUID, request: Request) -> dict[str, Any]:
    body = _parse(LeaseCredentials, await _json_body(request, 4096))
    return await run_in_threadpool(leases.release, str(drawing_id), body.lease_token, body.lease_generation)


@router.post("/drawings/{drawing_id}/mutations")
async def post_mutation(drawing_id: UUID, request: Request) -> dict[str, Any]:
    envelope = _parse(MutationEnvelope, await _json_body(request, settings.max_batch_bytes))
    return await run_in_threadpool(mutations.apply_mutation, str(drawing_id), envelope)


@router.get("/drawings/{drawing_id}/mutations/{mutation_id}")
def get_mutation(drawing_id: UUID, mutation_id: UUID) -> dict[str, Any]:
    found = mutations.receipt(str(drawing_id), str(mutation_id))
    if found is None:
        raise not_found("Mutation")
    return found


@router.get("/drawings/{drawing_id}/browser-progress")
def get_browser_progress(drawing_id: UUID, browser_id: UUID) -> dict[str, Any]:
    return progress.read_progress(str(drawing_id), str(browser_id))


@router.put("/drawings/{drawing_id}/browser-progress")
def put_browser_progress(drawing_id: UUID, body: BrowserProgressIn) -> dict[str, Any]:
    return progress.write_progress(str(drawing_id), body)


@router.post("/drawings/{drawing_id}/complete")
async def complete_drawing(drawing_id: UUID, request: Request) -> dict[str, Any]:
    body = _parse(CompleteRequest, await _json_body(request, 256 * 1024))
    return await run_in_threadpool(completion.complete, str(drawing_id), body)


@router.post("/drawings/{drawing_id}/reopen")
def reopen_drawing(drawing_id: UUID, body: ReopenRequest) -> dict[str, Any]:
    return completion.reopen(str(drawing_id), body)
