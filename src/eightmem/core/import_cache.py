from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from eightmem.core.paths import ensure_cache_dir


def import_cache_dir() -> Path:
    path = ensure_cache_dir() / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_import_cache_key(source_text: str, *, source_kind: str, source_id: str, options: dict[str, Any] | None = None) -> str:
    payload = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_text": source_text,
        "options": options or {},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_import_cache(key: str) -> dict[str, Any] | None:
    path = import_cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_import_cache(key: str, payload: dict[str, Any]) -> Path:
    path = import_cache_dir() / f"{key}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return path
