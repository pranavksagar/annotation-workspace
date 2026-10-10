"""Environment configuration for the annotation workspace (API, worker and tools)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


MIB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://annotation:annotation@127.0.0.1:55432/annotation")
    # Comma-separated list of origins the browser app is served from (scheme://host[:port]).
    app_public_origin: str = os.getenv("APP_PUBLIC_ORIGIN", "http://127.0.0.1:5174,http://localhost:5174")
    trusted_hosts: list[str] = field(default_factory=lambda: _list("TRUSTED_HOSTS", "127.0.0.1,localhost,testserver"))
    frontend_dist: str = os.getenv("FRONTEND_DIST", "")

    # Pool sizing is explicit so several web workers do not each open an oversized pool.
    db_pool_size: int = _int("DB_POOL_SIZE", 5)
    db_max_overflow: int = _int("DB_MAX_OVERFLOW", 5)

    max_upload_bytes: int = _int("MAX_UPLOAD_BYTES", 50 * MIB)
    max_pdf_pages: int = _int("MAX_PDF_PAGES", 100)
    max_page_pixels: int = _int("MAX_PAGE_PIXELS", 64_000_000)
    max_render_asset_bytes: int = _int("MAX_RENDER_ASSET_BYTES", 40 * MIB)
    max_total_render_bytes: int = _int("MAX_TOTAL_RENDER_BYTES", 1024 * MIB)
    render_dpi: float = _float("RENDER_DPI", 300)
    render_timeout_seconds: int = _int("RENDER_TIMEOUT_SECONDS", 120)
    render_memory_limit_mib: int = _int("RENDER_MEMORY_LIMIT_MIB", 4096)
    thumbnail_max_px: int = _int("THUMBNAIL_MAX_PX", 320)

    edit_lease_seconds: int = _int("EDIT_LEASE_SECONDS", 90)
    edit_heartbeat_seconds: int = _int("EDIT_HEARTBEAT_SECONDS", 20)

    worker_poll_seconds: float = _float("WORKER_POLL_SECONDS", 2)
    worker_heartbeat_seconds: float = _float("WORKER_HEARTBEAT_SECONDS", 10)
    worker_lease_seconds: int = _int("WORKER_LEASE_SECONDS", 60)
    worker_max_attempts: int = _int("WORKER_MAX_ATTEMPTS", 3)
    worker_concurrency: int = _int("WORKER_CONCURRENCY", 1)

    max_batch_operations: int = _int("MAX_BATCH_OPERATIONS", 200)
    max_batch_bytes: int = _int("MAX_BATCH_BYTES", 2 * MIB)
    max_regions_per_drawing: int = _int("MAX_REGIONS_PER_DRAWING", 50_000)
    max_groups_per_drawing: int = _int("MAX_GROUPS_PER_DRAWING", 10_000)
    max_concurrent_asset_reads: int = _int("MAX_CONCURRENT_ASSET_READS", 4)
    library_page_size: int = _int("LIBRARY_PAGE_SIZE", 30)

    enable_ocr_suggestions: bool = _bool("ENABLE_OCR_SUGGESTIONS", False)
    ocr_timeout_seconds: int = _int("OCR_TIMEOUT_SECONDS", 10)
    ocr_max_crop_pixels: int = _int("OCR_MAX_CROP_PIXELS", 4_000_000)
    ocr_concurrency: int = _int("OCR_CONCURRENCY", 2)
    ocr_language: str = os.environ.get("OCR_LANGUAGE", "eng")

    @property
    def allowed_origins(self) -> set[str]:
        from urllib.parse import urlsplit
        return {"{0.scheme}://{0.netloc}".format(urlsplit(item.strip()))
                for item in self.app_public_origin.split(",") if item.strip()}

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
