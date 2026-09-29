from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eightmem.core.paths import runtime_home
from eightmem.services.memory_service import build_public_agent_card_memory_state


DEFAULT_AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "viri": {
        "agent_name": "Viri",
        "runtime": "OpenClaw",
        "memory_user_id_env": "EIGHTMEM_VIRI_USER_ID",
        "sources": ["8mem", "OpenClaw/Viri"],
    },
    "govi": {
        "agent_name": "Govi",
        "runtime": "Hermes",
        "memory_user_id_env": "EIGHTMEM_GOVI_USER_ID",
        "sources": ["8mem", "Hermes/Govi"],
    },
}


class AgentCardRegistryError(ValueError):
    pass


def _registry_path() -> Path:
    configured = os.getenv("EIGHTMEM_AGENT_REGISTRY_PATH")
    if configured:
        return Path(configured).expanduser()
    return runtime_home() / "agent-card-agents.json"


def load_agent_registry() -> dict[str, dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return {agent_id: dict(profile) for agent_id, profile in DEFAULT_AGENT_REGISTRY.items()}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentCardRegistryError(f"Invalid AgentCard registry: {path}") from exc
    if not isinstance(raw, dict):
        raise AgentCardRegistryError("AgentCard registry must be a JSON object keyed by agent_id")

    registry: dict[str, dict[str, Any]] = {}
    for agent_id, profile in raw.items():
        if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(profile, dict):
            raise AgentCardRegistryError("Each AgentCard registry entry must be an object with a non-empty agent_id")
        registry[agent_id.strip().lower()] = dict(profile)
    return registry


def get_agent_profile(agent_id: str) -> dict[str, Any] | None:
    normalized_id = agent_id.strip().lower()
    profile = load_agent_registry().get(normalized_id)
    if profile is None:
        return None

    user_id = profile.get("memory_user_id")
    user_id_env = profile.get("memory_user_id_env")
    if not user_id and isinstance(user_id_env, str):
        user_id = os.getenv(user_id_env)
    if not user_id:
        user_id = os.getenv("EIGHTMEM_CONTEXT_USER_ID") or None

    sources = profile.get("sources")
    if not isinstance(sources, list) or not all(isinstance(source, str) and source.strip() for source in sources):
        sources = ["8mem", str(profile.get("runtime") or "agent runtime")]

    return {
        "agent_id": normalized_id,
        "agent_name": str(profile.get("agent_name") or normalized_id.title()),
        "owner": str(profile.get("owner") or os.getenv("EIGHTMEM_AGENT_OWNER") or "8mem"),
        "runtime": str(profile.get("runtime") or "unknown"),
        "memory_user_id": str(user_id) if user_id else None,
        "sources": list(dict.fromkeys(source.strip() for source in sources)),
    }


def build_agent_card_payload(agent_id: str) -> dict[str, Any] | None:
    profile = get_agent_profile(agent_id)
    if profile is None:
        return None
    state = build_public_agent_card_memory_state(user_id=profile["memory_user_id"])
    active_count = int(state["active_memory_count"])
    category_count = int(state["active_category_count"])
    memory_summary_public = [
        f"8mem maintains {active_count} active memory item{'s' if active_count != 1 else ''} across {category_count} categor{'ies' if category_count != 1 else 'y'}.",
        "Memory supports explicit correction, forgetting, duplicate protection, and portable context.",
    ]
    source_summary_public = [
        {
            "source": source,
            "status": "connected",
        }
        for source in profile["sources"]
    ]

    return {
        "agent_id": profile["agent_id"],
        "agent_name": profile["agent_name"],
        "owner": profile["owner"],
        "runtime": profile["runtime"],
        "memory_provider": "8mem",
        "memory_summary": memory_summary_public,
        "memory_summary_public": memory_summary_public,
        "recent_corrections": state["recent_corrections"],
        "source_summary": {
            "sources": profile["sources"],
            "recorded_change_count": state["recorded_change_count"],
            "last_memory_update": state["last_memory_update"],
        },
        "source_summary_public": source_summary_public,
        "correction_status": "changed" if state["recent_corrections"] else "current",
        "forget_status": "enforced",
        "last_memory_update": state["last_memory_update"],
        "engram_context_version": "v1",
        "redaction_status": "passed",
        "active_memory_count": active_count,
        "active_category_count": category_count,
        "capabilities": {
            "remember": True,
            "correct": True,
            "forget": True,
            "duplicate_protection": True,
            "passport": True,
            "engram_context": True,
        },
        "privacy": {
            "raw_memory_public": False,
            "correction_values_public": False,
            "public_payload_mode": "metadata_only",
        },
    }


def build_agent_card_health(agent_id: str) -> dict[str, Any] | None:
    payload = build_agent_card_payload(agent_id)
    if payload is None:
        return None
    checks = {
        "agent_registered": True,
        "identity_complete": all(payload.get(field) for field in ("agent_id", "agent_name", "owner", "runtime")),
        "public_redaction_enabled": payload["privacy"]["raw_memory_public"] is False,
        "correction_supported": payload["capabilities"]["correct"] is True,
        "forget_supported": payload["capabilities"]["forget"] is True,
        "engram_context_supported": payload["capabilities"]["engram_context"] is True,
    }
    return {
        "ok": all(checks.values()),
        "status": "ready" if all(checks.values()) else "needs_review",
        "agent_id": payload["agent_id"],
        "checks": checks,
    }
