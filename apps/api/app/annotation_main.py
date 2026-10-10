"""Annotation workspace entrypoint: `uvicorn apps.api.app.annotation_main:app`.

This app intentionally imports nothing from the legacy review application: no automatic model
reconciliation, no job recovery that fails other workers' jobs, no exports and no reviewer routes.
Migrations run as a separate deployment step (`alembic upgrade head`), never at startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .annotation.config import settings
from .annotation.errors import ApiError
from .annotation.router import router

log = logging.getLogger("annotation")

app = FastAPI(title="Drawing annotation workspace", version="1.0.0",
              openapi_url="/api/annotation/openapi.json", docs_url="/api/annotation/docs", redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_allowed_origins = settings.allowed_origins


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    if request.method in _UNSAFE and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin not in _allowed_origins:
            return JSONResponse(ApiError(403, "ORIGIN_REJECTED", "Requests must come from the application.").body(),
                                status_code=403)
        length = request.headers.get("content-length")
        limit = settings.max_upload_bytes + 1024 * 1024 if request.url.path.endswith("/drawings") \
            else settings.max_batch_bytes + 64 * 1024
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse(ApiError(413, "BODY_TOO_LARGE", "The request is too large.").body(), status_code=413)
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError) -> JSONResponse:
    headers = {}
    if exc.extra.get("retry_after_seconds"):
        headers["Retry-After"] = str(exc.extra["retry_after_seconds"])
    return JSONResponse(exc.body(), status_code=exc.status, headers=headers)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"code": "INVALID_FIELD", "message": e["msg"], "field": ".".join(str(p) for p in e["loc"])}
               for e in exc.errors()[:50]]
    return JSONResponse(ApiError(422, "INVALID_REQUEST", "The request is not valid.", details=details).body(),
                        status_code=422)


@app.exception_handler(OperationalError)
async def database_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
    log.warning("database unavailable: %s", type(exc).__name__)
    return JSONResponse(ApiError(503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable. Retrying…").body(),
                        status_code=503, headers={"Retry-After": "2"})


app.include_router(router)

# Serve the built frontend from the same origin (the container image sets FRONTEND_DIST).
_dist = Path(settings.frontend_dist) if settings.frontend_dist else None
if _dist and (_dist / "index.html").is_file():
    class HashedAssets(StaticFiles):
        """Bundle file names contain a content hash, so they can be cached for a year."""

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            if response.status_code == 200:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

    app.mount("/assets", HashedAssets(directory=_dist / "assets"), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
