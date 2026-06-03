from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from eightmem.core.notification_decision import NotificationDecision, decide_notification
from eightmem.core.paths import ensure_runtime_dirs, runtime_home, user_home
from eightmem.core.signal_connectors import ExternalSignal, SignalConnector, default_connectors


@dataclass
class HeartbeatRunResult:
    checked_at: str
    scanned_signals: int
    notifications: list[NotificationDecision]
    deferred: list[NotificationDecision]
    suppressed: list[NotificationDecision]
    state_path: str


def _state_path(user_id: str | None = None) -> Path:
    if user_id:
        home = user_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        return home / "heartbeat-state.json"
    home, _ = ensure_runtime_dirs()
    return home / "heartbeat-state.json"


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"seen_signal_ids": [], "last_checked_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_heartbeat(
    now: datetime | None = None,
    connectors: list[SignalConnector] | None = None,
    user_id: str | None = None,
) -> HeartbeatRunResult:
    current = now or datetime.now(timezone.utc)
    connector_list = connectors or default_connectors()
    path = _state_path(user_id)
    state = _load_state(path)
    seen_ids = set(str(item) for item in state.get("seen_signal_ids", []))

    notifications: list[NotificationDecision] = []
    deferred: list[NotificationDecision] = []
    suppressed: list[NotificationDecision] = []
    scanned = 0

    for connector in connector_list:
        for signal in connector.fetch(current, user_id=user_id):
            scanned += 1
            if signal.signal_id in seen_ids:
                continue
            decision = decide_notification(signal, current, user_id=user_id)
            if decision.delivery_mode == "notify_now":
                notifications.append(decision)
            elif decision.delivery_mode in {"defer", "digest"}:
                deferred.append(decision)
            else:
                suppressed.append(decision)
            seen_ids.add(signal.signal_id)

    state["seen_signal_ids"] = sorted(seen_ids)
    state["last_checked_at"] = current.isoformat()
    _save_state(path, state)

    return HeartbeatRunResult(
        checked_at=current.isoformat(),
        scanned_signals=scanned,
        notifications=notifications,
        deferred=deferred,
        suppressed=suppressed,
        state_path=str(path),
    )
