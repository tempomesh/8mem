from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from eightmem.core.paths import ensure_runtime_dirs, runtime_home, user_home


SignalUrgency = str


@dataclass
class ExternalSignal:
    signal_id: str
    source: str
    title: str
    summary: str
    urgency: SignalUrgency = "normal"
    starts_at: str | None = None
    sender: str | None = None
    tags: list[str] = field(default_factory=list)


class SignalConnector(Protocol):
    name: str

    def fetch(self, now: datetime, user_id: str | None = None) -> list[ExternalSignal]:
        ...


def _signals_dir(user_id: str | None = None) -> Path:
    if user_id:
        base = user_home(user_id)
        base.mkdir(parents=True, exist_ok=True)
    else:
        base, _ = ensure_runtime_dirs()
    path = base / "signals"
    path.mkdir(parents=True, exist_ok=True)
    return path


class FileSignalConnector:
    def __init__(self, name: str, filename: str) -> None:
        self.name = name
        self.filename = filename

    def fetch(self, now: datetime, user_id: str | None = None) -> list[ExternalSignal]:
        path = _signals_dir(user_id) / self.filename
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        signals: list[ExternalSignal] = []
        for item in data:
            signals.append(
                ExternalSignal(
                    signal_id=str(item["signal_id"]),
                    source=str(item.get("source", self.name)),
                    title=str(item["title"]),
                    summary=str(item.get("summary", "")),
                    urgency=str(item.get("urgency", "normal")),
                    starts_at=item.get("starts_at"),
                    sender=item.get("sender"),
                    tags=[str(tag) for tag in item.get("tags", [])],
                )
            )
        return signals


def default_connectors() -> list[SignalConnector]:
    return [
        FileSignalConnector("email", "email_signals.json"),
        FileSignalConnector("calendar", "calendar_signals.json"),
    ]
