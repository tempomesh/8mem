from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from eightmem.core.paths import ensure_runtime_dirs


CONNECTORS_FILE_NAME = "connectors.json"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
SUPPORTED_EVENTS = {"memory.created", "memory.corrected", "memory.forgotten"}
logger = logging.getLogger(__name__)


@dataclass
class Connector:
    id: str
    url: str
    secret: str
    description: str | None
    user_id: str | None
    enabled: bool
    registered_at: str
    updated_at: str | None = None


def connectors_path() -> Path:
    home, _ = ensure_runtime_dirs()
    return home / CONNECTORS_FILE_NAME


def list_connectors(*, user_id: str | None = None, include_disabled: bool = False) -> list[Connector]:
    connectors = _load_connectors()
    result: list[Connector] = []
    for connector in connectors:
        if not include_disabled and not connector.enabled:
            continue
        if user_id is not None and connector.user_id not in {None, user_id}:
            continue
        result.append(connector)
    return sorted(result, key=lambda item: item.registered_at)


def register_connector(
    *,
    connector_id: str,
    url: str,
    description: str | None = None,
    user_id: str | None = None,
    secret: str | None = None,
) -> tuple[Connector | None, str | None]:
    connector_id = connector_id.strip()
    url = url.strip()
    if not _valid_connector_id(connector_id):
        return None, "Connector id must use letters, numbers, dot, underscore, or hyphen."
    if not _valid_connector_url(url):
        return None, "Connector url must be http or https."

    now = _now_iso()
    connectors = _load_connectors()
    existing_by_id = {connector.id: connector for connector in connectors}
    existing = existing_by_id.get(connector_id)
    connector = Connector(
        id=connector_id,
        url=url,
        secret=secret.strip() if isinstance(secret, str) and secret.strip() else secrets.token_hex(32),
        description=description.strip() if isinstance(description, str) and description.strip() else None,
        user_id=user_id,
        enabled=True,
        registered_at=existing.registered_at if existing is not None else now,
        updated_at=now if existing is not None else None,
    )
    existing_by_id[connector_id] = connector
    _save_connectors(list(existing_by_id.values()))
    return connector, None


def deregister_connector(connector_id: str, *, user_id: str | None = None) -> bool:
    connectors = _load_connectors()
    kept: list[Connector] = []
    removed = False
    for connector in connectors:
        if connector.id == connector_id and (user_id is None or connector.user_id in {None, user_id}):
            removed = True
            continue
        kept.append(connector)
    if removed:
        _save_connectors(kept)
    return removed


def public_connector(connector: Connector) -> dict[str, Any]:
    data = asdict(connector)
    data.pop("secret", None)
    return data


def notify_connectors(event: str, *, user_id: str | None = None) -> int:
    if event not in SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported webhook event: {event}")

    payload_user_id = user_id or "default"
    payload: dict[str, Any] = {"event": event, "user_id": payload_user_id, "timestamp": int(time.time())}
    try:
        from eightmem.services.memory_service import build_engram_context

        context = build_engram_context(user_id=user_id)
        if isinstance(context, dict) and isinstance(context.get("system_prompt_injection"), str):
            payload["system_prompt_injection"] = context["system_prompt_injection"]
    except Exception:
        pass
    connectors = [
        connector
        for connector in list_connectors(user_id=user_id)
        if connector.enabled
    ]
    for connector in connectors:
        _start_push_thread(connector, payload)
    return len(connectors)


def build_signed_request(connector: Connector, payload: dict[str, Any]) -> urllib.request.Request:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = sign_payload(body, connector.secret)
    return urllib.request.Request(
        connector.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-8mem-Signature": signature,
            "X-8mem-Timestamp": timestamp,
        },
        method="POST",
    )


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _start_push_thread(connector: Connector, payload: dict[str, Any]) -> None:
    threading.Thread(target=_push_one, args=(connector, payload), daemon=True).start()


def _push_one(connector: Connector, payload: dict[str, Any]) -> None:
    for attempt in range(MAX_RETRIES):
        request = build_signed_request(connector, payload)
        try:
            with urllib.request.urlopen(request, timeout=5):
                return
        except (OSError, urllib.error.URLError) as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            logger.warning(
                "8mem_connector_push_failed connector_id=%s event=%s url=%s attempts=%s error=%s",
                connector.id,
                payload.get("event", "unknown"),
                connector.url,
                MAX_RETRIES,
                exc,
            )


def _load_connectors() -> list[Connector]:
    path = connectors_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    connectors: list[Connector] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            connectors.append(Connector(**item))
        except TypeError:
            continue
    return connectors


def _save_connectors(connectors: list[Connector]) -> None:
    path = connectors_path()
    path.write_text(
        json.dumps([asdict(connector) for connector in connectors], ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _valid_connector_id(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    return all(char.isalnum() or char in {"-", "_", "."} for char in value)


def _valid_connector_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
