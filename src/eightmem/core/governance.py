from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
import uuid

from eightmem.core.paths import USERS_DIR, ensure_runtime_dirs, runtime_home, user_home


ActionPolicy = Literal["allow", "allow_if_urgent", "ask_first", "deny"]
NotificationPolicy = Literal["notify_now", "notify_if_urgent", "defer", "digest", "suppress"]
UrgencyLevel = Literal["critical", "high", "normal", "low"]

GOVERNANCE_FILE = "governance.json"
APPROVALS_FILE = "approvals.json"
APPROVAL_EVENTS_FILE = "approval_events.jsonl"
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]

DEFAULT_APPROVAL_TTL_SECONDS = 900
APPROVAL_TTL_BY_ACTION_TYPE: dict[str, int] = {
    "email_send": 300,
    "external_delete": 120,
    "file_delete": 120,
    "calendar_write": 900,
    "calendar_update": 900,
    "public_post": 600,
}


@dataclass
class GovernancePolicy:
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    urgent_overrides_quiet_hours: bool = True
    action_policies: dict[str, ActionPolicy] = field(
        default_factory=lambda: {
            "draft": "allow",
            "summary": "allow",
            "email_send": "ask_first",
            "calendar_notify": "allow_if_urgent",
            "public_post": "ask_first",
            "external_delete": "deny",
        }
    )
    notification_policies: dict[str, NotificationPolicy] = field(
        default_factory=lambda: {
            "email": "notify_if_urgent",
            "calendar": "notify_if_urgent",
            "memory_review": "digest",
            "system": "notify_now",
        }
    )


@dataclass
class GovernanceDecision:
    capability: str
    permission: ActionPolicy
    notification: NotificationPolicy
    urgency: UrgencyLevel
    reason: str
    requires_confirmation: bool


@dataclass
class ApprovalRequest:
    approval_id: str
    user_id: str | None
    action_type: str
    summary: str
    payload: dict[str, Any]
    status: ApprovalStatus
    created_at: str
    expires_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    reason: str | None = None
    prompt: str | None = None


def _governance_path(user_id: str | None = None) -> Path:
    if user_id:
        home = user_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        return home / GOVERNANCE_FILE
    home, _ = ensure_runtime_dirs()
    return home / GOVERNANCE_FILE


def _approval_store_path(user_id: str | None = None) -> Path:
    if user_id:
        home = user_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        return home / APPROVALS_FILE
    home, _ = ensure_runtime_dirs()
    return home / APPROVALS_FILE


def _approval_events_path(user_id: str | None = None) -> Path:
    if user_id:
        home = user_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        return home / APPROVAL_EVENTS_FILE
    home, _ = ensure_runtime_dirs()
    return home / APPROVAL_EVENTS_FILE


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _is_within_quiet_hours(now: datetime, policy: GovernancePolicy) -> bool:
    start_h, start_m = _parse_time(policy.quiet_hours_start)
    end_h, end_m = _parse_time(policy.quiet_hours_end)
    current = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start < end:
        return start <= current < end
    return current >= start or current < end


def ensure_governance_policy(user_id: str | None = None) -> GovernancePolicy:
    path = _governance_path(user_id)
    if not path.exists():
        policy = GovernancePolicy()
        path.write_text(json.dumps(asdict(policy), indent=2), encoding="utf-8")
        return policy
    return load_governance_policy(user_id)


def load_governance_policy(user_id: str | None = None) -> GovernancePolicy:
    path = _governance_path(user_id)
    if not path.exists():
        return ensure_governance_policy(user_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return GovernancePolicy(**data)


def save_governance_policy(policy: GovernancePolicy, user_id: str | None = None) -> Path:
    path = _governance_path(user_id)
    path.write_text(json.dumps(asdict(policy), indent=2), encoding="utf-8")
    return path


def evaluate_action(
    action_name: str,
    urgency: UrgencyLevel,
    now: datetime,
    user_id: str | None = None,
) -> GovernanceDecision:
    policy = load_governance_policy(user_id)
    permission = policy.action_policies.get(action_name, "ask_first")
    in_quiet_hours = _is_within_quiet_hours(now, policy)
    if permission == "allow":
        notification: NotificationPolicy = "notify_now"
        reason = f"{action_name} is allowed by policy."
        return GovernanceDecision("available", permission, notification, urgency, reason, False)
    if permission == "allow_if_urgent":
        if urgency in {"critical", "high"}:
            notification = "notify_now"
            reason = f"{action_name} is allowed because urgency is {urgency}."
            return GovernanceDecision("available", permission, notification, urgency, reason, False)
        notification = "defer" if in_quiet_hours else "digest"
        reason = f"{action_name} is allowed only for urgent cases; current urgency is {urgency}."
        return GovernanceDecision("available", permission, notification, urgency, reason, False)
    if permission == "deny":
        return GovernanceDecision("available", permission, "suppress", urgency, f"{action_name} is denied by policy.", False)
    notification = "defer" if in_quiet_hours else "notify_now"
    reason = f"{action_name} requires confirmation before execution."
    return GovernanceDecision("available", permission, notification, urgency, reason, True)


def evaluate_notification(
    signal_kind: str,
    urgency: UrgencyLevel,
    now: datetime,
    user_id: str | None = None,
) -> GovernanceDecision:
    policy = load_governance_policy(user_id)
    notification = policy.notification_policies.get(signal_kind, "digest")
    in_quiet_hours = _is_within_quiet_hours(now, policy)
    is_urgent = urgency in {"critical", "high"}
    if in_quiet_hours and not (policy.urgent_overrides_quiet_hours and is_urgent):
        return GovernanceDecision(
            capability="available",
            permission="allow",
            notification="defer",
            urgency=urgency,
            reason="Signal deferred because it falls within quiet hours.",
            requires_confirmation=False,
        )
    if notification == "notify_if_urgent":
        if is_urgent:
            return GovernanceDecision(
                capability="available",
                permission="allow",
                notification="notify_now",
                urgency=urgency,
                reason=f"Signal is urgent, so it can notify now for {signal_kind}.",
                requires_confirmation=False,
            )
        return GovernanceDecision(
            capability="available",
            permission="allow",
            notification="digest",
            urgency=urgency,
            reason=f"Signal kept for digest because urgency is {urgency}.",
            requires_confirmation=False,
        )
    if notification == "suppress":
        return GovernanceDecision("available", "allow", "suppress", urgency, "Signal suppressed by policy.", False)
    return GovernanceDecision(
        capability="available",
        permission="allow",
        notification=notification,
        urgency=urgency,
        reason=f"Signal follows {notification} policy for {signal_kind}.",
        requires_confirmation=False,
    )


def create_approval_request(
    *,
    action_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    user_id: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> ApprovalRequest:
    current = _coerce_aware_datetime(now)
    ttl = normalize_approval_ttl(action_type, ttl_seconds)
    approval = ApprovalRequest(
        approval_id=str(uuid.uuid4()),
        user_id=user_id,
        action_type=action_type,
        summary=summary,
        payload=payload or {},
        status="pending",
        created_at=current.isoformat(timespec="seconds"),
        expires_at=(current + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
        prompt=_build_approval_prompt(action_type, summary),
    )
    approvals = _load_approvals(user_id)
    approvals[approval.approval_id] = approval
    _save_approvals(user_id, approvals)
    _append_approval_event(user_id, "created", approval)
    return approval


def get_approval_request(
    approval_id: str,
    *,
    user_id: str | None = None,
    now: datetime | None = None,
) -> ApprovalRequest | None:
    approval, _ = _find_approval_request(approval_id, user_id=user_id, now=now)
    return approval


def _find_approval_request(
    approval_id: str,
    *,
    user_id: str | None = None,
    now: datetime | None = None,
) -> tuple[ApprovalRequest | None, str | None]:
    for candidate_user_id in _approval_lookup_user_ids(user_id):
        approvals = _load_approvals(candidate_user_id)
        approval = approvals.get(approval_id)
        if approval is None:
            continue
        if approval.status == "pending" and _coerce_aware_datetime(now) >= datetime.fromisoformat(approval.expires_at):
            approval.status = "expired"
            approval.resolved_at = _coerce_aware_datetime(now).isoformat(timespec="seconds")
            approval.reason = "Approval request expired."
            approvals[approval_id] = approval
            _save_approvals(candidate_user_id, approvals)
            _append_approval_event(candidate_user_id, "expired", approval)
        return approval, candidate_user_id
    return None, user_id


def _approval_lookup_user_ids(user_id: str | None = None) -> list[str | None]:
    if user_id:
        return [user_id]

    candidates: list[str | None] = [None]
    users_dir = runtime_home() / USERS_DIR
    if not users_dir.exists():
        return candidates

    for child in sorted(users_dir.iterdir()):
        if child.is_dir() and (child / APPROVALS_FILE).exists():
            candidates.append(child.name)
    return candidates


def list_approval_requests(
    *,
    user_id: str | None = None,
    status: ApprovalStatus | None = None,
    now: datetime | None = None,
) -> list[ApprovalRequest]:
    refreshed: list[ApprovalRequest] = []
    for candidate_user_id in _approval_lookup_user_ids(user_id):
        approvals = _load_approvals(candidate_user_id)
        for approval_id in list(approvals):
            approval = get_approval_request(approval_id, user_id=candidate_user_id, now=now)
            if approval is None:
                continue
            if status is None or approval.status == status:
                refreshed.append(approval)
    return sorted(refreshed, key=lambda item: item.created_at, reverse=True)


def normalize_approval_ttl(action_type: str, ttl_seconds: int | None = None) -> int:
    if ttl_seconds is None:
        ttl_seconds = APPROVAL_TTL_BY_ACTION_TYPE.get(action_type, DEFAULT_APPROVAL_TTL_SECONDS)
    return max(30, min(ttl_seconds, 24 * 60 * 60))


def resolve_approval_request(
    approval_id: str,
    *,
    decision: Literal["approved", "denied"],
    user_id: str | None = None,
    resolved_by: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[ApprovalRequest | None, str | None]:
    approval, store_user_id = _find_approval_request(approval_id, user_id=user_id, now=now)
    if approval is None:
        return None, "not_found"
    if approval.status != "pending":
        return approval, "already_resolved"
    approvals = _load_approvals(store_user_id)
    current = _coerce_aware_datetime(now)
    approval.status = decision
    approval.resolved_at = current.isoformat(timespec="seconds")
    approval.resolved_by = resolved_by or "runtime"
    approval.reason = reason
    approvals[approval_id] = approval
    _save_approvals(store_user_id, approvals)
    _append_approval_event(store_user_id, decision, approval)
    return approval, None


def _load_approvals(user_id: str | None = None) -> dict[str, ApprovalRequest]:
    path = _approval_store_path(user_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    approvals: dict[str, ApprovalRequest] = {}
    for approval_id, value in raw.items():
        if isinstance(approval_id, str) and isinstance(value, dict):
            try:
                approvals[approval_id] = ApprovalRequest(**value)
            except TypeError:
                continue
    return approvals


def _save_approvals(user_id: str | None, approvals: dict[str, ApprovalRequest]) -> None:
    path = _approval_store_path(user_id)
    serializable = {approval_id: asdict(approval) for approval_id, approval in approvals.items()}
    path.write_text(json.dumps(serializable, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def _append_approval_event(user_id: str | None, action: str, approval: ApprovalRequest) -> None:
    path = _approval_events_path(user_id)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "approval": asdict(approval),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _coerce_aware_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _build_approval_prompt(action_type: str, summary: str) -> str:
    return (
        "Approval requested.\n\n"
        f"Action: {action_type}\n"
        f"Summary: {summary}\n\n"
        "Approve only if this action should run outside 8mem."
    )
