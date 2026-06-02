"""Server-side registry for uploaded .pbix files (patch-measures workflow)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_PBIX_UPLOADS: dict[str, dict[str, Any]] = {}
_UPLOAD_MAX_AGE_SEC = 3600


def upload_dir() -> Path:
    root = Path(__file__).resolve().parent / "tmp" / "pbix_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_old_uploads(max_age_seconds: int = _UPLOAD_MAX_AGE_SEC) -> None:
    """Remove uploads older than max_age_seconds from memory and disk."""
    now = time.time()
    expired_ids: list[str] = []
    for pbix_id, meta in list(_PBIX_UPLOADS.items()):
        if now - float(meta.get("timestamp") or 0) > max_age_seconds:
            expired_ids.append(pbix_id)

    for pbix_id in expired_ids:
        unregister_upload(pbix_id)

    # Orphan files on disk (e.g. after server restart)
    cutoff = now - max_age_seconds
    for path in upload_dir().glob("*.pbix"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def register_upload(pbix_id: str, file_path: Path, original_filename: str) -> None:
    cleanup_old_uploads()
    _PBIX_UPLOADS[pbix_id] = {
        "id": pbix_id,
        "original_filename": original_filename,
        "file_path": str(file_path.resolve()),
        "timestamp": time.time(),
    }


def get_upload(pbix_id: str) -> dict[str, Any] | None:
    cleanup_old_uploads()
    return _PBIX_UPLOADS.get(pbix_id)


def unregister_upload(pbix_id: str) -> None:
    meta = _PBIX_UPLOADS.pop(pbix_id, None)
    if not meta:
        return
    try:
        Path(meta["file_path"]).unlink(missing_ok=True)
    except OSError:
        pass
