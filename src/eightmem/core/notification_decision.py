from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from eightmem.core.governance import GovernanceDecision, evaluate_notification
from eightmem.core.signal_connectors import ExternalSignal


@dataclass
class NotificationDecision:
    signal_id: str
    source: str
    title: str
    summary: str
    urgency: str
    delivery_mode: str
    should_notify: bool
    reason: str


def decide_notification(
    signal: ExternalSignal,
    now: datetime,
    user_id: str | None = None,
) -> NotificationDecision:
    policy: GovernanceDecision = evaluate_notification(signal.source, signal.urgency, now, user_id=user_id)
    return NotificationDecision(
        signal_id=signal.signal_id,
        source=signal.source,
        title=signal.title,
        summary=signal.summary,
        urgency=signal.urgency,
        delivery_mode=policy.notification,
        should_notify=policy.notification == "notify_now",
        reason=policy.reason,
    )
