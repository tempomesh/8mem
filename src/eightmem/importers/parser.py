from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eightmem.core.models import ChatMessage


ROLE_KEYS = ("role", "speaker", "author", "from")
TEXT_KEYS = ("content", "text", "message", "body", "prompt", "response")


def load_messages(path: Path) -> list[ChatMessage]:
    if not path.exists():
        raise FileNotFoundError(f"Chat export not found: {path}")

    if path.suffix.lower() in {".json", ".jsonl"}:
        content = json.loads(path.read_text(encoding="utf-8"))
        return _extract_from_json(content)

    text = path.read_text(encoding="utf-8")
    return _extract_from_text(text)


def _extract_from_json(content: Any) -> list[ChatMessage]:
    messages: list[ChatMessage] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return

        if not isinstance(node, dict):
            return

        role = _first_string(node, ROLE_KEYS) or "unknown"
        text = _first_string(node, TEXT_KEYS)
        if text:
            messages.append(ChatMessage(role=role.lower(), text=text.strip()))

        for value in node.values():
            if isinstance(value, (list, dict)):
                walk(value)

    walk(content)
    return [m for m in messages if m.text]


def _first_string(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_from_text(text: str) -> list[ChatMessage]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    messages: list[ChatMessage] = []
    for line in lines:
        if ":" in line and len(line.split(":", 1)[0]) < 20:
            role, body = line.split(":", 1)
            messages.append(ChatMessage(role=role.strip().lower(), text=body.strip()))
        else:
            messages.append(ChatMessage(role="unknown", text=line))
    return messages
