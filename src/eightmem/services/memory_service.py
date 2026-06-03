from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eightmem.analyzers.extractor import _extract_corrections, _normalize_clause, _render_directive_preference
from eightmem.core.constants import MEMORY_FILES, MEMORY_ORDER
from eightmem.core.context_export import compile_context
from eightmem.core.memory_store import _parse_bullets, _render, update_memory_files
from eightmem.core.paths import ensure_runtime_dirs, ensure_user_runtime_dirs, memory_dir, user_memory_dir
from eightmem.core.sqlite_facts import (
    append_memory_event_to_sqlite,
    read_active_derived_facts,
    read_active_sections_from_sqlite,
    search_semantic_memory,
    sqlite_has_active_facts,
    sqlite_vec_available,
    sync_memory_dir_to_sqlite,
    sync_memory_file_to_sqlite,
)
from eightmem.core.templates import copy_default_templates, template_dir
from eightmem.llm.ollama import generate_text, resolve_base_url, resolve_default_model
from eightmem.mirror.summary import _detect_tensions, build_mirror_text, mirror_sections


DEFAULT_USER_ID = "default"
EVENT_LOG_NAME = ".memory_events.jsonl"
PENDING_CONTRADICTION_NAME = ".pending_contradiction.json"
PENDING_MEMORY_PROPOSALS_NAME = ".memory_proposals.json"
DEDUP_REVIEW_FILES = {"IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md", "DECISIONS.md"}
DEDUP_FALLBACK_FILE_PRIORITY = ["IDENTITY.md", "PREFERENCES.md", "BELIEFS.md", "CORRECTIONS.md", "DECISIONS.md"]
MEMORY_DISPLAY: dict[str, dict[str, str]] = {
    "IDENTITY.md": {
        "label": "Bio & Profile",
        "short": "Profile",
        "desc": "Who you are, what you do, and what you are focused on.",
    },
    "BELIEFS.md": {
        "label": "Learned Facts",
        "short": "Facts",
        "desc": "Useful facts and patterns your AI can carry forward.",
    },
    "PREFERENCES.md": {
        "label": "My Preferences",
        "short": "Preferences",
        "desc": "Tone, format, style, and working preferences.",
    },
    "CORRECTIONS.md": {
        "label": "Corrections & Fixes",
        "short": "Corrections",
        "desc": "Persistent fixes that stop repeated mistakes.",
    },
    "DECISIONS.md": {
        "label": "Decisions",
        "short": "Decisions",
        "desc": "Important choices and commitments worth preserving.",
    },
    "EVOLUTION.md": {
        "label": "Timeline & History",
        "short": "Timeline",
        "desc": "How your context and preferences have changed.",
    },
}
MEMORY_FILE_SLUGS = {
    "IDENTITY.md": "about-you",
    "BELIEFS.md": "things-learned",
    "PREFERENCES.md": "preferences",
    "CORRECTIONS.md": "corrections",
    "EVOLUTION.md": "changes-over-time",
    "DECISIONS.md": "decisions",
}
MEMORY_FILE_BY_SLUG = {slug: name for name, slug in MEMORY_FILE_SLUGS.items()}


def _memory_display(name: str) -> dict[str, str]:
    return MEMORY_DISPLAY.get(
        name,
        {
            "label": name.replace(".md", "").title(),
            "short": name.replace(".md", "").title(),
            "desc": MEMORY_FILES.get(name, "Saved memory."),
        },
    )


def public_memory_file_slug(name: str) -> str:
    return MEMORY_FILE_SLUGS.get(name, name)


def resolve_public_memory_file_name(value: str) -> str:
    normalized = value.strip()
    return MEMORY_FILE_BY_SLUG.get(normalized, normalized)


def initialize_memory_runtime(user_id: str | None = None) -> Path:
    if user_id:
        _, mem = ensure_user_runtime_dirs(user_id)
    else:
        _, mem = ensure_runtime_dirs()
    copy_default_templates(mem)
    _canonicalize_existing_entries(mem, {"IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md"})
    _sync_memory_dir_state(mem)
    return mem


def get_memory_dir(user_id: str | None = None) -> Path:
    return initialize_memory_runtime(user_id)


def get_user_memory_dir(user_id: str) -> Path:
    return initialize_memory_runtime(user_id)


def get_retrieval_sections(user_id: str | None = None) -> dict[str, list[str]]:
    return _read_sections_for_retrieval(get_memory_dir(user_id))


def get_structured_tensions(user_id: str | None = None) -> list[str]:
    return _read_structured_tensions(get_memory_dir(user_id))


def build_base_context(page: str, user_id: str | None = None) -> dict[str, object]:
    mem = initialize_memory_runtime(user_id)
    sections = mirror_sections(mem)
    entries = {name: sections.get(name, []) for name in MEMORY_ORDER}
    entry_count = sum(len(items) for items in entries.values())
    counts = {
        "identity": len(entries["IDENTITY.md"]),
        "preferences": len(entries["PREFERENCES.md"]),
        "corrections": len(entries["CORRECTIONS.md"]),
        "decisions": len(entries["DECISIONS.md"]),
        "evolution": len(entries["EVOLUTION.md"]),
    }
    active_scope = {
        "kind": "user-scoped" if user_id else "default",
        "label": f"Telegram / user {user_id}" if user_id else "Default local memory",
        "user_id": user_id,
        "query_suffix": f"?user_id={user_id}" if user_id else "",
        "pill": "User-scoped memory" if user_id else "Default memory",
        "note": (
            "You are viewing memory for one channel user only."
            if user_id
            else "You are viewing the default local memory set."
        ),
    }
    structured = build_structured_memory_summary(user_id)
    return {
        "active_page": page,
        "memory_dir": str(mem),
        "sections": entries,
        "counts": counts,
        "entry_count": entry_count,
        "mirror_text": build_mirror_text(mem),
        "active_scope": active_scope,
        "undo": get_undo_payload(user_id=user_id),
        "structured": structured,
    }


def load_dashboard_payload(user_id: str | None = None) -> dict[str, object]:
    context = build_base_context("dashboard", user_id)
    sections = context["sections"]
    assert isinstance(sections, dict)
    tensions = _read_structured_tensions(get_memory_dir(user_id))
    memory_state = [
        {
            "name": name,
            "label": _memory_display(name)["label"],
            "short": _memory_display(name)["short"],
            "desc": MEMORY_FILES[name],
            "friendly_desc": _memory_display(name)["desc"],
            "preview": sections.get(name, [])[:1],
        }
        for name in MEMORY_ORDER
    ]
    timeline = _build_memory_timeline(get_memory_dir(user_id), sections)
    health = _build_memory_health(sections, tensions)
    resolutions = _build_resolution_suggestions(tensions, user_id=user_id)
    return {
        **context,
        "memory_state": memory_state[:3],
        "timeline": timeline,
        "health": health,
        "resolutions": resolutions,
        "trust_basis": context["structured"]["trust_basis"] if isinstance(context.get("structured"), dict) else [],
    }


def get_mirror_payload(user_id: str | None = None) -> dict[str, object]:
    context = build_base_context("mirror", user_id)
    mem = get_memory_dir(user_id)
    files = {name: (mem / name).read_text(encoding="utf-8") for name in MEMORY_ORDER}
    tensions = _read_structured_tensions(mem)
    sections = context["sections"]
    assert isinstance(sections, dict)
    return {
        **context,
        "tensions": tensions,
        "health": _build_memory_health(sections, tensions),
        "resolutions": _build_resolution_suggestions(tensions, user_id=user_id),
        "trust_basis": context["structured"]["trust_basis"] if isinstance(context.get("structured"), dict) else [],
    }


def get_memory_inbox_payload(user_id: str | None = None) -> dict[str, object]:
    context = build_base_context("inbox", user_id)
    sections = context["sections"]
    assert isinstance(sections, dict)
    tensions = _read_structured_tensions(get_memory_dir(user_id))
    contradiction_items = _build_resolution_suggestions(tensions, user_id=user_id)
    return {
        **context,
        "health": _build_memory_health(sections, tensions),
        "items": [*_build_memory_proposal_inbox_items(user_id), *contradiction_items],
    }


def get_memory_resolution_suggestions(user_id: str | None = None) -> list[dict[str, str]]:
    context = build_base_context("telegram", user_id=user_id)
    sections = context["sections"]
    assert isinstance(sections, dict)
    tensions = _read_structured_tensions(get_memory_dir(user_id))
    return _build_resolution_suggestions(tensions, user_id=user_id)


def get_files_overview(user_id: str | None = None) -> dict[str, object]:
    context = build_base_context("files", user_id)
    sections = context["sections"]
    assert isinstance(sections, dict)
    files = []
    for name in MEMORY_ORDER:
        items = sections.get(name, [])
        files.append(
            {
                "name": name,
                "slug": public_memory_file_slug(name),
                "label": _memory_display(name)["label"],
                "short": _memory_display(name)["short"],
                "desc": MEMORY_FILES[name],
                "friendly_desc": _memory_display(name)["desc"],
                "count": len(items),
                "preview": items[:1],
            }
        )
    return {**context, "files": files}


def get_memory_file_path(name: str, user_id: str | None = None) -> Path | None:
    if name not in MEMORY_ORDER:
        return None
    mem = get_memory_dir(user_id)
    path = mem / name
    if not path.exists():
        return None
    return path


def get_file_content(name: str, user_id: str | None = None) -> str | None:
    path = get_memory_file_path(name, user_id)
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def get_file_editor_payload(name: str, user_id: str | None = None) -> dict[str, object] | None:
    content = get_file_content(name, user_id)
    if content is None:
        return None
    context = build_base_context("editor", user_id)
    files = [
        {
            "name": item,
            "slug": public_memory_file_slug(item),
            "label": _memory_display(item)["label"],
            "active": item == name,
        }
        for item in MEMORY_ORDER
    ]
    return {
        **context,
        "name": name,
        "slug": public_memory_file_slug(name),
        "label": _memory_display(name)["label"],
        "content": content,
        "files": files,
    }


def save_file_content(name: str, content: str, user_id: str | None = None) -> bool:
    path = get_memory_file_path(name, user_id)
    if path is None:
        return False
    before = _parse_bullets(path.read_text(encoding="utf-8"))
    path.write_text(content, encoding="utf-8")
    after = _parse_bullets(content)
    sync_memory_file_to_sqlite(get_memory_dir(user_id), name, after)
    _record_diff_event(get_memory_dir(user_id), action="edit_file", file_name=name, before=before, after=after)
    return True


def get_undo_payload(user_id: str | None = None) -> dict[str, str] | None:
    event = _find_last_reversible_event(get_memory_dir(user_id))
    if not event:
        return None
    detail = str(event.get("detail", "")).strip()
    if not detail:
        detail = "Undo the most recent memory change."
    if _contains_internal_runtime_jargon(detail):
        detail = "Latest memory update is ready. You can undo it if needed."
    return {
        "label": "Undo last memory change",
        "detail": detail,
    }


def _contains_internal_runtime_jargon(value: str) -> bool:
    lowered = value.lower()
    return any(
        phrase in lowered
        for phrase in {
            "openclaw",
            "hermes",
            "viri",
            "govi",
            "agents.md",
            "soul.md",
            "write-back",
            "runtime hook",
            "gateway",
        }
    )


def undo_last_memory_change(user_id: str | None = None) -> bool:
    mem = get_memory_dir(user_id)
    event = _find_last_reversible_event(mem)
    if not event:
        return False

    action = str(event.get("action", ""))
    if action == "reset_memory":
        before_files = event.get("before_files")
        if not isinstance(before_files, dict):
            return False
        for name in MEMORY_ORDER:
            content = before_files.get(name)
            if isinstance(content, str):
                (mem / name).write_text(content, encoding="utf-8")
        _record_memory_event(
            mem,
            action="undo_change",
            detail=f"Undid memory reset and restored the previous memory state.",
        )
        return True

    file_name = event.get("file")
    before = event.get("before")
    if not isinstance(file_name, str) or not isinstance(before, list):
        return False
    path = mem / file_name
    path.write_text(_render(file_name, [str(item) for item in before]), encoding="utf-8")
    _record_memory_event(
        mem,
        action="undo_change",
        file_name=file_name,
        detail=f"Undid last change in {file_name.replace('.md', '').title()}.",
    )
    return True


def resolve_memory_inbox_item(item_id: str, decision: str, user_id: str | None = None) -> bool:
    if item_id.startswith("memory_proposal:"):
        proposal_id = item_id.split(":", 1)[1]
        proposal_decision = {
            "save": "approved",
            "skip": "denied",
            "approved": "approved",
            "denied": "denied",
        }.get(decision)
        if proposal_decision is None:
            return False
        result = commit_memory_proposal(user_id=user_id, proposal_id=proposal_id, decision=proposal_decision)
        return bool(result.get("ok"))

    mem = get_memory_dir(user_id)
    if item_id == "emoji_guidance":
        if decision == "keep_correction":
            return _remove_entries_matching(mem, "PREFERENCES.md", "emoji")
        if decision == "keep_preference":
            return _remove_entries_matching(mem, "CORRECTIONS.md", "emoji")
        return False
    if item_id == "detail_conflict":
        if decision == "keep_concise":
            return _remove_entries_matching_any(
                mem,
                "PREFERENCES.md",
                ["detailed", "detail", "long detailed explanations", "long explanations", "full explanations"],
            )
        if decision == "keep_detailed":
            return _remove_entries_matching_any(mem, "PREFERENCES.md", ["keep answers concise", "concise", "short answers"])
        return False
    if item_id == "format_conflict":
        if decision == "keep_bullets":
            return _remove_entries_matching_any(
                mem,
                "PREFERENCES.md",
                [
                    "write in long narrative paragraphs",
                    "long narrative paragraphs",
                    "long detailed explanations",
                    "long explanations",
                    "give detailed explanations",
                    "detailed explanations",
                ],
            )
        if decision == "keep_long_form":
            return _remove_entries_matching_any(mem, "PREFERENCES.md", ["structured", "bullets", "bullet"])
        return False
    return False


def apply_memory_updates(updates: dict[str, set[str]], user_id: str | None = None) -> dict[str, int]:
    mem = get_memory_dir(user_id)
    _canonicalize_existing_entries(mem, updates.keys())
    before = {
        name: _parse_bullets((mem / name).read_text(encoding="utf-8")) if (mem / name).exists() else []
        for name in updates.keys()
    }
    stats = update_memory_files(mem, updates)
    for name in updates.keys():
        after = _parse_bullets((mem / name).read_text(encoding="utf-8")) if (mem / name).exists() else []
        _record_diff_event(mem, action="update_file", file_name=name, before=before.get(name, []), after=after)
    return stats


def write_canonical_memory(
    *,
    user_id: str | None,
    file_name: str,
    value: str,
    source: str | None = None,
    confidence: str | None = None,
) -> dict[str, Any]:
    """Write one active memory through the shared dedup/conflict gate."""
    if _is_memory_injection_artifact(value):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "injection_artifact",
            "user_id": user_id,
        }

    duplicate = find_duplicate_active_memory(user_id, value)
    if duplicate:
        return {
            "ok": False,
            "status": "duplicate",
            "error": "Near-duplicate memory detected",
            "conflict": duplicate,
            "proposed": {"file": file_name, "value": value},
        }

    refinement = find_refinement_active_memory(user_id, value, target_file=None if file_name == "BELIEFS.md" else file_name)
    if refinement:
        return {
            "ok": False,
            "status": "refinement_pending",
            "error": "Memory refinement needs confirmation",
            "conflict": refinement,
            "existing": refinement,
            "proposed": {"file": file_name, "value": value},
        }

    conflict = find_conflicting_active_memory(user_id, value, target_file=None if file_name == "BELIEFS.md" else file_name)
    if conflict:
        return {
            "ok": False,
            "status": "conflict",
            "error": "Memory conflict detected",
            "conflict": conflict,
            "proposed": {"file": file_name, "value": value},
        }

    apply_memory_updates({file_name: {value}}, user_id=user_id)
    return {
        "ok": True,
        "status": "written",
        "user_id": user_id,
        "memory": {
            "id": runtime_memory_id(user_id, file_name, value),
            "file": file_name,
            "value": value,
            "source": source,
            "confidence": confidence,
        },
    }


def create_memory_proposal(
    *,
    user_id: str | None,
    file_name: str,
    value: str,
    source: str | None = None,
    confidence: str | None = None,
    source_message: str | None = None,
) -> dict[str, Any]:
    """Create a pending memory proposal without writing active memory."""
    if _is_memory_injection_artifact(value):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "injection_artifact",
            "user_id": user_id,
        }

    duplicate = find_duplicate_active_memory(user_id, value)
    if duplicate:
        return {
            "ok": False,
            "status": "duplicate",
            "error": "Near-duplicate memory detected",
            "conflict": duplicate,
            "proposed": {"file": file_name, "value": value},
        }

    refinement = find_refinement_active_memory(user_id, value, target_file=None if file_name == "BELIEFS.md" else file_name)
    if refinement:
        return {
            "ok": False,
            "status": "refinement_pending",
            "error": "Memory refinement needs confirmation",
            "conflict": refinement,
            "existing": refinement,
            "proposed": {"file": file_name, "value": value},
        }

    conflict = find_conflicting_active_memory(user_id, value, target_file=None if file_name == "BELIEFS.md" else file_name)
    if conflict:
        return {
            "ok": False,
            "status": "conflict",
            "error": "Memory conflict detected",
            "conflict": conflict,
            "proposed": {"file": file_name, "value": value},
        }

    mem = get_memory_dir(user_id)
    proposals = _load_memory_proposals(mem)
    normalized_new = _normalize_memory_duplicate_key(value)
    for existing in proposals:
        if existing.get("status") != "pending":
            continue
        existing_value = str(existing.get("value") or "")
        if _normalize_memory_duplicate_key(existing_value) == normalized_new:
            return {"ok": True, "status": "pending", "user_id": user_id, "proposal": existing}

    proposal = {
        "id": f"prop_{uuid.uuid4().hex[:16]}",
        "status": "pending",
        "user_id": user_id,
        "file": file_name,
        "value": value,
        "source": source,
        "confidence": confidence,
        "source_message": source_message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    proposals.append(proposal)
    _save_memory_proposals(mem, proposals)
    _record_memory_event(
        mem,
        action="memory_proposed",
        file_name=file_name,
        detail=f"Proposed memory for {file_name.replace('.md', '').title()}: {value}",
        new_value=value,
        new_file=file_name,
        source_message=source_message,
        status="pending",
    )
    return {"ok": True, "status": "proposed", "user_id": user_id, "proposal": proposal}


def propose_natural_memory_candidate(
    *,
    user_id: str | None,
    message: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Write a high-trust natural user statement directly to memory."""
    candidate = _natural_preference_candidate(message)
    if candidate is None:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "not_high_trust_candidate",
            "user_id": user_id,
        }
    return write_canonical_memory(
        user_id=user_id,
        file_name="PREFERENCES.md",
        value=candidate,
        source=source or "runtime_candidate",
        confidence="inferred_durable_preference",
    )


def commit_memory_proposal(
    *,
    user_id: str | None,
    proposal_id: str,
    decision: str,
) -> dict[str, Any]:
    """Approve or deny a pending memory proposal."""
    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "approved", "save", "commit", "deny", "denied", "reject", "skip"}:
        return {"ok": False, "status": "invalid_decision", "error": "decision must approve or deny"}

    mem = get_memory_dir(user_id)
    proposals = _load_memory_proposals(mem)
    for proposal in proposals:
        if proposal.get("id") != proposal_id:
            continue
        if proposal.get("status") != "pending":
            return {"ok": False, "status": "not_pending", "error": "Proposal is not pending", "proposal": proposal}

        if normalized_decision in {"deny", "denied", "reject", "skip"}:
            proposal["status"] = "denied"
            proposal["resolved_at"] = datetime.now(timezone.utc).isoformat()
            _save_memory_proposals(mem, proposals)
            _record_memory_event(
                mem,
                action="memory_proposal_denied",
                file_name=str(proposal.get("file") or ""),
                detail=f"Denied memory proposal: {proposal.get('value')}",
                new_value=str(proposal.get("value") or ""),
                new_file=str(proposal.get("file") or ""),
                status="denied",
            )
            return {"ok": True, "status": "denied", "user_id": user_id, "proposal": proposal}

        result = write_canonical_memory(
            user_id=user_id,
            file_name=str(proposal.get("file") or "BELIEFS.md"),
            value=str(proposal.get("value") or ""),
            source=str(proposal.get("source") or "") or None,
            confidence=str(proposal.get("confidence") or "") or None,
        )
        if result.get("status") == "written":
            proposal["status"] = "committed"
            proposal["resolved_at"] = datetime.now(timezone.utc).isoformat()
            proposal["memory"] = result.get("memory")
            _save_memory_proposals(mem, proposals)
            _record_memory_event(
                mem,
                action="memory_proposal_committed",
                file_name=str(proposal.get("file") or ""),
                detail=f"Committed memory proposal: {proposal.get('value')}",
                new_value=str(proposal.get("value") or ""),
                new_file=str(proposal.get("file") or ""),
                source_message=str(proposal.get("source_message") or ""),
                status="committed",
            )
        return result | {"proposal": proposal}

    return {"ok": False, "status": "not_found", "error": "Proposal not found"}


def runtime_memory_id(user_id: str | None, file_name: str, value: str) -> str:
    scope = user_id or DEFAULT_USER_ID
    digest = hashlib.sha256(f"{scope}\0{file_name}\0{value.lower()}".encode("utf-8")).hexdigest()
    return f"mem_{digest[:16]}"


def find_duplicate_active_memory(user_id: str | None, new_value: str) -> dict[str, str] | None:
    normalized_new = _normalize_memory_duplicate_key(new_value)
    if len(normalized_new) < 12:
        return None
    sections = _safe_retrieval_sections(user_id)
    all_values = [
        item for file_items in sections.values()
        for item in file_items
        if isinstance(item, str) and item.strip() and not _is_deletion_audit_memory(item)
    ]
    for existing in all_values:
        existing = existing.strip()
        if not existing:
            continue
        normalized_existing = _normalize_memory_duplicate_key(existing)
        if normalized_existing == normalized_new:
            return {"file_name": "", "value": existing}
        relation = _memory_semantic_relation(existing, new_value)
        if relation == "duplicate":
            return {"file_name": "", "value": existing}
    return None


def _is_deletion_audit_memory(value: str) -> bool:
    """Deletion audit lines are history, not active facts for duplicate blocking."""
    return "deleted memory from " in " ".join(value.lower().split())


def _safe_retrieval_sections(user_id: str | None) -> dict[str, list[str]]:
    """Read active memory even when SQLite sync is temporarily unavailable."""
    try:
        return get_retrieval_sections(user_id)
    except Exception:
        mem = user_memory_dir(user_id) if user_id else memory_dir()
        sections: dict[str, list[str]] = {}
        for file_name in MEMORY_ORDER:
            path = mem / file_name
            sections[file_name] = _parse_bullets(path.read_text(encoding="utf-8")) if path.exists() else []
        return sections


def find_refinement_active_memory(
    user_id: str | None,
    new_value: str,
    *,
    target_file: str | None = None,
) -> dict[str, str] | None:
    sections = _safe_retrieval_sections(user_id)
    candidate_files = [target_file] if target_file else ["PREFERENCES.md", "CORRECTIONS.md", "IDENTITY.md", "BELIEFS.md"]
    for file_name in candidate_files:
        if not file_name:
            continue
        for item in sections.get(file_name, []):
            if _memory_semantic_relation(item, new_value) == "refinement":
                return {"file_name": file_name, "value": item}
    return None


def preview_memory_dedup(user_id: str | None = None) -> dict[str, Any]:
    """Return exact duplicate cleanup actions and near-duplicate review items."""
    mem = get_memory_dir(user_id)
    rows = _active_dedup_rows(mem)
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(row["normalized"], []).append(row)

    exact_actions: list[dict[str, Any]] = []
    for normalized, items in groups.items():
        if len(items) < 2:
            continue
        keep = _choose_dedup_keeper(items)
        remove = [item for item in items if item is not keep]
        exact_actions.append(
            {
                "reason": "exact_normalized_duplicate",
                "normalized": normalized,
                "keep": {"file": keep["file"], "value": keep["value"]},
                "remove": [{"file": item["file"], "value": item["value"]} for item in remove],
            }
        )

    near_review: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["normalized"] == right["normalized"]:
                continue
            if min(len(left["normalized"]), len(right["normalized"])) < 12:
                continue
            ratio = _duplicate_similarity(left["normalized"], right["normalized"])
            containment = left["normalized"] in right["normalized"] or right["normalized"] in left["normalized"]
            if ratio >= 0.82 or containment:
                near_review.append(
                    {
                        "reason": "near_duplicate_review",
                        "similarity": round(ratio, 3),
                        "left": {"file": left["file"], "value": left["value"]},
                        "right": {"file": right["file"], "value": right["value"]},
                    }
                )

    return {
        "user_id": user_id,
        "exact_duplicates": exact_actions,
        "near_duplicates": near_review,
        "exact_duplicate_count": sum(len(action["remove"]) for action in exact_actions),
        "near_duplicate_count": len(near_review),
    }


def apply_memory_dedup(user_id: str | None = None) -> dict[str, Any]:
    """Apply exact duplicate cleanup only; near duplicates remain human-review items."""
    mem = get_memory_dir(user_id)
    preview = preview_memory_dedup(user_id=user_id)
    removed: list[dict[str, str]] = []
    for action in preview["exact_duplicates"]:
        for item in action["remove"]:
            file_name = str(item["file"])
            value = str(item["value"])
            if _remove_exact_memory_entry(mem, file_name, value):
                removed.append({"file": file_name, "value": value})
    if removed:
        _record_memory_event(
            mem,
            action="memory_dedup_cleanup",
            detail=f"Removed {len(removed)} exact duplicate active memory entries.",
            status="applied",
        )
    return {
        **preview,
        "status": "applied" if removed else "unchanged",
        "removed": removed,
        "removed_count": len(removed),
    }


def apply_trusted_memory_change(
    *,
    user_id: str | None,
    file_name: str,
    new_value: str,
    trigger: str,
    source_message: str,
    old_value: str | None = None,
    category: str | None = None,
) -> dict[str, str | None]:
    """Apply a user-confirmed memory change and record an auditable old -> new event."""
    mem = get_memory_dir(user_id)
    _canonicalize_existing_entries(mem, {"IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md"})
    target_file = None if file_name == "CORRECTIONS.md" else file_name
    active_old = _find_exact_active_memory(user_id, old_value) if old_value else None
    if active_old is None:
        active_old = find_conflicting_active_memory(user_id, new_value, target_file=target_file)
    if active_old is None and old_value:
        active_old = {"file_name": file_name, "value": old_value}
    if active_old:
        _remove_exact_memory_entry(mem, active_old["file_name"], active_old["value"])

    apply_memory_updates({file_name: {new_value}}, user_id=user_id)
    event = {
        "trigger": trigger,
        "category": category or infer_memory_category(new_value),
        "old_value": active_old["value"] if active_old else old_value,
        "old_file": active_old["file_name"] if active_old else None,
        "new_value": new_value,
        "new_file": file_name,
        "source_message": source_message,
        "status": "active",
    }
    _record_memory_event(
        mem,
        action="trusted_memory_change",
        file_name=file_name,
        detail=_format_trusted_change_detail(event),
        **event,
    )
    return event


def _find_exact_active_memory(user_id: str | None, value: str) -> dict[str, str] | None:
    normalized = _normalize_memory_duplicate_key(value)
    if not normalized:
        return None
    sections = _safe_retrieval_sections(user_id)
    for file_name in ("PREFERENCES.md", "CORRECTIONS.md", "IDENTITY.md", "BELIEFS.md"):
        for item in sections.get(file_name, []):
            if _normalize_memory_duplicate_key(item) == normalized:
                return {"file_name": file_name, "value": item}
    return None


def build_corrections_summary(user_id: str | None = None) -> str:
    mem = get_memory_dir(user_id)
    events = [
        event
        for event in _load_memory_events(mem)
        if (
            event.get("action") == "trusted_memory_change"
            and event.get("trigger") in {"correction", "contradiction"}
        )
        or event.get("action") == "forget_memory"
    ]
    if not events:
        return "No corrections yet.\n\nWhen you correct something, I'll show the change here."

    lines = ["Corrections"]
    for event in reversed(events[-8:]):
        ts = _format_event_date(str(event.get("ts", "")))
        category = str(event.get("category") or "memory")
        if event.get("action") == "forget_memory":
            old_value = str(event.get("old_value") or "").strip()
            if old_value:
                lines.append(f'{ts}  {category}: deleted "{old_value}"')
            continue
        old_value = event.get("old_value")
        new_value = str(event.get("new_value") or "").strip()
        if old_value:
            lines.append(f'{ts}  {category}: "{old_value}" -> "{new_value}"')
        else:
            lines.append(f'{ts}  {category}: added "{new_value}"')
    return "\n".join(lines)


def find_forget_candidates(
    query: str,
    *,
    user_id: str | None = None,
    file_name: str | None = None,
) -> list[dict[str, str]]:
    """Return active memory entries that can be deleted by an explicit user request."""
    needle = " ".join(query.lower().split())
    if not needle:
        return []
    normalized_needle = _normalize_memory_duplicate_key(query)
    needle_tokens = set(_semantic_memory_tokens(query))
    candidate_files = [file_name] if file_name else ["IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md"]
    sections = get_retrieval_sections(user_id)
    matches: list[dict[str, str]] = []
    for candidate_file in candidate_files:
        if candidate_file not in MEMORY_ORDER:
            continue
        for value in sections.get(candidate_file, []):
            haystack = " ".join(value.lower().split())
            normalized_haystack = _normalize_memory_duplicate_key(value)
            haystack_tokens = set(_semantic_memory_tokens(value))
            normalized_match = bool(
                normalized_needle
                and (
                    normalized_needle in normalized_haystack
                    or (len(needle_tokens) >= 2 and needle_tokens.issubset(haystack_tokens))
                )
            )
            if needle in haystack or normalized_match:
                matches.append({"file_name": candidate_file, "value": value})
    return matches


def forget_memory_entries(
    query: str,
    *,
    user_id: str | None = None,
    file_name: str | None = None,
) -> list[dict[str, str]]:
    """Delete matching active memory entries and record an auditable deletion event."""
    mem = get_memory_dir(user_id)
    matches = find_forget_candidates(query, user_id=user_id, file_name=file_name)
    deleted: list[dict[str, str]] = []
    for match in matches:
        target_file = match["file_name"]
        value = match["value"]
        path = mem / target_file
        before = _parse_bullets(path.read_text(encoding="utf-8")) if path.exists() else []
        after = [item for item in before if item != value]
        if after == before:
            continue
        path.write_text(_render(target_file, after), encoding="utf-8")
        sync_memory_file_to_sqlite(mem, target_file, after)
        evolution_path = mem / "EVOLUTION.md"
        evolution_before = _parse_bullets(evolution_path.read_text(encoding="utf-8")) if evolution_path.exists() else []
        evolution_entry = f'Deleted memory from {target_file.replace(".md", "").title()}: "{value}"'
        evolution_after = list(evolution_before)
        _append_unique(evolution_after, evolution_entry)
        if evolution_after != evolution_before:
            evolution_path.write_text(_render("EVOLUTION.md", evolution_after), encoding="utf-8")
            sync_memory_file_to_sqlite(mem, "EVOLUTION.md", evolution_after)
        category = infer_memory_category(value)
        detail = evolution_entry
        _record_memory_event(
            mem,
            action="forget_memory",
            file_name=target_file,
            detail=detail,
            before=before,
            after=after,
            trigger="deletion",
            category=category,
            old_value=value,
            old_file=target_file,
            status="deleted",
        )
        deleted.append(match)
    return deleted


def find_conflicting_active_memory(
    user_id: str | None,
    new_value: str,
    *,
    target_file: str | None = None,
) -> dict[str, str] | None:
    sections = _safe_retrieval_sections(user_id)
    new_signals = _memory_conflict_signals(new_value)
    if not new_signals:
        return None

    # Conflict checks must scan current active memory, not deletion/history notes.
    # EVOLUTION.md can contain old deleted values and should not block new writes.
    candidate_files = [target_file] if target_file else ["PREFERENCES.md", "CORRECTIONS.md", "IDENTITY.md", "BELIEFS.md"]
    for file_name in candidate_files:
        if not file_name:
            continue
        for item in sections.get(file_name, []):
            if _memory_values_conflict(new_signals, _memory_conflict_signals(item)):
                return {"file_name": file_name, "value": item}
    return None


def save_pending_contradiction(
    *,
    user_id: str | None,
    file_name: str,
    new_value: str,
    old_file: str,
    old_value: str,
    source_message: str,
) -> None:
    mem = get_memory_dir(user_id)
    payload = {
        "file_name": file_name,
        "new_value": new_value,
        "old_file": old_file,
        "old_value": old_value,
        "category": infer_memory_category(new_value),
        "source_message": source_message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (mem / PENDING_CONTRADICTION_NAME).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def load_pending_contradiction(user_id: str | None) -> dict[str, str] | None:
    path = get_memory_dir(user_id) / PENDING_CONTRADICTION_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    if not isinstance(raw, dict):
        return None
    required = {"file_name", "new_value", "old_file", "old_value", "source_message"}
    if not all(isinstance(raw.get(key), str) and raw.get(key) for key in required):
        return None
    return {key: str(value) for key, value in raw.items() if isinstance(value, str)}


def clear_pending_contradiction(user_id: str | None) -> None:
    (get_memory_dir(user_id) / PENDING_CONTRADICTION_NAME).unlink(missing_ok=True)


def _memory_proposals_path(memory_dir: Path) -> Path:
    return memory_dir / PENDING_MEMORY_PROPOSALS_NAME


def _load_memory_proposals(memory_dir: Path) -> list[dict[str, Any]]:
    path = _memory_proposals_path(memory_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _save_memory_proposals(memory_dir: Path, proposals: list[dict[str, Any]]) -> None:
    _memory_proposals_path(memory_dir).write_text(json.dumps(proposals, ensure_ascii=True, indent=2), encoding="utf-8")


def _natural_preference_candidate(message: str) -> str | None:
    text = " ".join(message.strip().split())
    lowered = text.lower().replace("’", "’")
    if not text or len(text) > 280 or text.startswith("/") or "?" in text:
        return None
    if any(
        marker in lowered
        for marker in (
            "api key",
            "bearer ",
            "password",
            "secret",
            "token",
            "localhost",
            "127.0.0.1",
            "/users/",
            "/home/",
            "diagnosed",
            "medical",
            "religion",
            "politics",
        )
    ):
        return None

    # Explicit preference patterns
    for prefix in ("i usually want ", "i usually prefer ", "i generally want ", "i generally prefer "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"I prefer {text[len(prefix):]}")
    for prefix in ("i usually avoid ", "i generally avoid "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"Avoid {text[len(prefix):]}")
    for prefix in ("i usually do ", "i generally do "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"I prefer {text[len(prefix):]}")
    for prefix in ("i work best ", "i tend to ", "i often "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"I prefer to {text[len(prefix):]}")

    # Natural personal fact patterns — preference, identity, habit, correction
    for prefix in ("my favourite ", "my favorite "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"My favourite {text[len(prefix):]}")
    for prefix in ("i prefer ", "i like ", "i love ", "i enjoy ", "i hate ", "i dislike ", "i don’t like ", "i do not like "):
        if lowered.startswith(prefix):
            return normalize_preference_text(f"I prefer {text[len(prefix):]}" if prefix in ("i prefer ",) else text)
    for prefix in ("i am ", "i’m "):
        if lowered.startswith(prefix):
            return normalize_preference_text(text)
    for prefix in ("i work ", "i live ", "i use ", "i drink ", "i eat ", "i wake ", "i sleep "):
        if lowered.startswith(prefix):
            return normalize_preference_text(text)
    for prefix in ("my name is ", "my timezone is ", "my role is ", "my job is ", "my company is "):
        if lowered.startswith(prefix):
            return normalize_preference_text(text)

    return None


def _build_memory_proposal_inbox_items(user_id: str | None) -> list[dict[str, str]]:
    proposals = _load_memory_proposals(get_memory_dir(user_id))
    items: list[dict[str, str]] = []
    for proposal in reversed(proposals):
        if proposal.get("status") != "pending":
            continue
        value = str(proposal.get("value") or "").strip()
        if not value:
            continue
        source = str(proposal.get("source") or "connected agent").replace("_", " ")
        items.append(
            {
                "id": f"memory_proposal:{proposal.get('id')}",
                "kind": "proposal",
                "title": "Possible memory",
                "reason": value,
                "note": f"Observed from {source}. Save only if this should shape future agent behavior.",
            }
        )
    return items


def _is_memory_injection_artifact(value: str) -> bool:
    normalized = " ".join(value.strip().lower().split())
    return normalized.startswith(
        (
            "user is ",
            "timezone:",
            "language:",
            "the user's name is ",
        )
    )


def _normalize_memory_duplicate_key(value: str) -> str:
    return " ".join(_semantic_memory_tokens(value))


def _semantic_memory_tokens(value: str) -> list[str]:
    lowered = value.lower()
    replacements = {
        "favourite": "favorite",
        "colour": "color",
        "drinks": "drink",
        "prefers": "prefer",
        "likes": "like",
    }
    for old, new in replacements.items():
        lowered = lowered.replace(old, new)
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in lowered)
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "be",
        "for",
        "i",
        "is",
        "my",
        "the",
        "to",
    }
    return [token for token in cleaned.split() if token and token not in stop_words]


def _memory_semantic_relation(existing_value: str, new_value: str) -> str | None:
    existing_subject = _memory_subject_value(existing_value)
    new_subject = _memory_subject_value(new_value)
    if existing_subject and new_subject:
        existing_key, existing_detail = existing_subject
        new_key, new_detail = new_subject
        if existing_key != new_key:
            return None
        existing_tokens = set(_semantic_memory_tokens(existing_detail))
        new_tokens = set(_semantic_memory_tokens(new_detail))
        if not existing_tokens or not new_tokens:
            return None
        overlap = existing_tokens & new_tokens
        if existing_tokens == new_tokens or new_tokens.issubset(existing_tokens):
            return "duplicate"
        if existing_tokens.issubset(new_tokens):
            return "refinement"
        if len(overlap) >= max(1, min(len(existing_tokens), len(new_tokens)) - 1):
            if len(new_tokens) > len(existing_tokens):
                return "refinement"
            if new_tokens.issubset(existing_tokens):
                return "duplicate"
        return None

    existing_tokens = set(_semantic_memory_tokens(existing_value))
    new_tokens = set(_semantic_memory_tokens(new_value))
    if len(existing_tokens) < 2 or len(new_tokens) < 2:
        return None
    if new_tokens.issubset(existing_tokens):
        return "duplicate"
    if existing_tokens.issubset(new_tokens) and len(new_tokens - existing_tokens) <= 3:
        return "refinement"
    return None


def _memory_subject_value(value: str) -> tuple[str, str] | None:
    normalized = " ".join(_semantic_memory_tokens(value))
    subject_markers = {
        "favorite drink": "favorite_drink",
        "favorite color": "favorite_color",
        "test word": "test_word",
        "launch smoke color": "launch_smoke_color",
    }
    for marker, subject in subject_markers.items():
        if marker not in normalized:
            continue
        detail = normalized.split(marker, 1)[1].strip()
        if detail:
            return subject, detail
    return None


def _active_dedup_rows(memory_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for file_name in MEMORY_ORDER:
        if file_name not in DEDUP_REVIEW_FILES:
            continue
        path = memory_dir / file_name
        if not path.exists():
            continue
        for value in _parse_bullets(path.read_text(encoding="utf-8")):
            normalized = _normalize_memory_duplicate_key(value)
            if normalized:
                rows.append({"file": file_name, "value": value, "normalized": normalized})
    return rows


def _choose_dedup_keeper(items: list[dict[str, str]]) -> dict[str, str]:
    files = {item["file"] for item in items}
    canonical = _canonical_file_for_duplicate(items[0]["value"])
    if canonical in files:
        for item in items:
            if item["file"] == canonical:
                return item
    for preferred in DEDUP_FALLBACK_FILE_PRIORITY:
        for item in items:
            if item["file"] == preferred:
                return item
    return items[0]


def _canonical_file_for_duplicate(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith(("do not ", "don't ", "never ", "avoid assuming ", "do n't ")):
        return "CORRECTIONS.md"
    if lowered.startswith(
        (
            "prefers ",
            "values ",
            "keep ",
            "give ",
            "use ",
            "write in ",
            "respond in ",
            "avoid ",
            "current location:",
            "timezone ",
        )
    ):
        return "PREFERENCES.md"
    if lowered.startswith(("user is ", "name ", "identity ", "role ")):
        return "IDENTITY.md"
    if lowered.startswith(("decided ", "decision ", "will launch ", "launch date ")):
        return "DECISIONS.md"
    return "BELIEFS.md"


def _duplicate_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return 0.0
    shared = len(set(left_tokens) & set(right_tokens))
    return (2 * shared) / (len(set(left_tokens)) + len(set(right_tokens)))


def infer_memory_category(value: str) -> str:
    signals = _memory_conflict_signals(value)
    if "emoji_allow" in signals or "emoji_deny" in signals:
        return "communication style"
    if signals & {"concise", "detailed", "bullets", "long_form"}:
        return "communication style"
    if signals & {"morning", "late_night"}:
        return "work timing"
    if "timezone" in signals or any(item.startswith("timezone:") for item in signals):
        return "timezone"
    if "location" in signals or any(item.startswith("location:") for item in signals):
        return "location"
    if any(item.startswith("default_tool:") for item in signals):
        return "tool preference"
    return "memory"


def export_context_text(user_id: str | None = None) -> str:
    mem = get_memory_dir(user_id)
    return compile_context(mem)


def generate_memory_aware_reply(
    user_id: str | None,
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    compiled_context = export_context_text(user_id)
    semantic_block = _build_semantic_context_block(get_memory_dir(user_id), prompt)
    style_instructions = _build_style_control_instructions(user_id, prompt, draft_type="generic")
    full_prompt = (
        "Use the memory context below to answer accurately. Keep the response concise.\n\n"
        f"{style_instructions}"
        f"{compiled_context}\n"
        f"{semantic_block}"
        f"User question: {prompt}"
    )
    result = generate_text(
        model=resolve_default_model(model),
        prompt=full_prompt,
        base_url=resolve_base_url(base_url),
        timeout_seconds=timeout_seconds,
    )
    return _clean_reply_response(result.text, user_id=user_id, prompt=prompt)


def generate_generic_reply(
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    full_prompt = (
        "Answer the user's request directly without using any saved memory or personalization.\n"
        "Keep the response concise and practical.\n"
        "Do not mention memory, profiles, or personalization.\n\n"
        f"User question: {prompt}"
    )
    result = generate_text(
        model=resolve_default_model(model),
        prompt=full_prompt,
        base_url=resolve_base_url(base_url),
        timeout_seconds=timeout_seconds,
    )
    return result.text


def generate_memory_aware_draft(
    user_id: str | None,
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    compiled_context = export_context_text(user_id)
    semantic_block = _build_semantic_context_block(get_memory_dir(user_id), prompt)
    draft_type = _classify_draft_request(prompt)
    if templated := _generate_direct_draft_template(user_id, prompt, draft_type=draft_type):
        return templated
    if draft_type == "founder_update" and _is_generic_founder_update_request(prompt):
        progress, risk, next_step = _memory_grounded_founder_update(user_id)
        return _render_founder_update(progress, risk, next_step, user_id=user_id)
    if draft_type == "summarize" and _is_underspecified_summarize_request(prompt):
        return _memory_grounded_summarize_clarification(user_id)
    draft_hints = _build_draft_hints(user_id, prompt, draft_type)
    type_instructions = _draft_type_instructions(draft_type)
    style_instructions = _build_style_control_instructions(user_id, prompt, draft_type=draft_type)
    full_prompt = (
        "Use the memory context below to write the requested draft directly.\n"
        "Do not output memory headings, markdown section titles, analysis, or reasoning.\n"
        "Do not mention IDENTITY, BELIEFS, PREFERENCES, CORRECTIONS, EVOLUTION, or DECISIONS.\n"
        "Do not begin with phrases like 'Here is', 'Here's', or 'Below is'.\n"
        "Write only the draft the user asked for.\n"
        "Respect the stored corrections as hard constraints.\n"
        "If the request says short, keep the answer to at most 4 short sentences.\n"
        "Prefer direct, concrete language over generic executive filler.\n\n"
        f"{type_instructions}"
        f"{draft_hints}"
        f"{style_instructions}"
        f"{compiled_context}\n"
        f"{semantic_block}"
        f"Draft request: {prompt}"
    )
    result = generate_text(
        model=resolve_default_model(model),
        prompt=full_prompt,
        base_url=resolve_base_url(base_url),
        timeout_seconds=timeout_seconds,
    )
    return _clean_draft_response(result.text, draft_type=draft_type, user_id=user_id)


def generate_generic_draft(
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    draft_type = _classify_draft_request(prompt)
    if templated := _generate_direct_draft_template(None, prompt, draft_type=draft_type):
        return templated
    if draft_type == "founder_update":
        progress, risk, next_step = _memory_grounded_founder_update(None)
        return "\n".join(
            [
                f"- Progress: {progress}",
                f"- Risk: {risk}",
                f"- Next: {next_step}",
            ]
        )
    if draft_type == "summarize" and _is_underspecified_summarize_request(prompt):
        return _generic_summarize_clarification()

    full_prompt = (
        "Write the requested draft directly without using any saved memory or personalization.\n"
        "Do not output analysis or reasoning.\n"
        "Do not mention memory, profiles, or personalization.\n"
        "If the request is underspecified, stay generic and avoid invented specifics.\n\n"
        f"{_draft_type_instructions(draft_type)}"
        f"Draft request: {prompt}"
    )
    result = generate_text(
        model=resolve_default_model(model),
        prompt=full_prompt,
        base_url=resolve_base_url(base_url),
        timeout_seconds=timeout_seconds,
    )
    return _clean_draft_response(result.text, draft_type=draft_type, user_id=None)


def build_passport_summary(user_id: str | None) -> str:
    mem = get_memory_dir(user_id)
    sections = _read_sections_for_retrieval(mem)
    preferences = sections.get("PREFERENCES.md", [])
    corrections = sections.get("CORRECTIONS.md", [])
    identity = sections.get("IDENTITY.md", [])
    tensions = _read_structured_tensions(mem)
    derived = read_active_derived_facts(mem)
    health = _build_memory_health(sections, tensions)

    lines = [
        "8mem Passport",
        "",
        "Here's the memory I'm using for you:",
        "",
        f"Status: {health['label']}",
        f"Active entries: {health['entry_count']}",
        f"Corrections tracked: {len(corrections)}",
        "",
        "Currently using:",
    ]
    active_start = len(lines)
    if identity:
        lines.append(f"- Identity: {identity[0]}")
    if current_location := derived.get("person.location.current", []):
        lines.append(f"- Current location: {current_location[0]}")
    if previous_location := derived.get("person.location.previous", []):
        lines.append(f"- Previous location: {previous_location[0]}")
    for item in preferences[:3]:
        lines.append(f"- Preference: {item}")
    for item in corrections[:2]:
        lines.append(f"- Correction: {item}")
    if len(lines) == active_start:
        lines.append("- Nothing yet. Add one preference or correction to start shaping replies.")

    lines.append("")
    if tensions:
        lines.append(f"Memory status: {health['label']} ({len(tensions)} tension{'s' if len(tensions) != 1 else ''})")
        lines.append("Needs review:")
        lines.extend(f"- {item}" for item in tensions[:3])
    else:
        lines.append(f"Memory status: {health['label']}")
    passport_basis = _build_passport_basis(sections, derived, tensions)
    if passport_basis:
        lines.append("")
        lines.append("Trust basis:")
        lines.extend(f"- {item}" for item in passport_basis)

    if health["status"] == "empty":
        lines.append("")
        lines.append("Next step:")
        lines.append('- Try: Remember: keep answers concise')
    return "\n".join(lines)


def build_recent_changes_summary(user_id: str | None = None) -> str:
    mem = get_memory_dir(user_id)
    events = _load_memory_events(mem)
    if not events:
        return "I don't have any saved changes for you yet."
    last_reset_idx = max((idx for idx, event in enumerate(events) if event.get("action") == "reset_memory"), default=-1)
    recent_events = events[last_reset_idx:] if last_reset_idx >= 0 else events
    active_values = _active_timeline_values(_read_sections_for_retrieval(mem))
    suppressed_low_level_values = _trusted_change_values(recent_events)
    meaningful: list[str] = []
    for event in reversed(recent_events):
        detail = _normalize_recent_change_detail(
            event,
            active_values=active_values,
            suppressed_low_level_values=suppressed_low_level_values,
        )
        if detail:
            meaningful.append(detail)
    if not meaningful:
        return "I don't have any saved changes for you yet."

    lines = ["What changed in your memory:"]
    for item in meaningful[:4]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def build_recent_changes_payload(user_id: str | None = None, *, limit: int = 20) -> dict[str, object]:
    mem = get_memory_dir(user_id)
    events = _load_memory_events(mem)
    normalized_events: list[dict[str, object]] = []
    for event in reversed(events[-max(1, limit) :]):
        normalized_events.append(
            {
                "ts": event.get("ts"),
                "action": event.get("action"),
                "file": event.get("file"),
                "detail": event.get("detail"),
                "trigger": event.get("trigger"),
                "category": event.get("category"),
                "old_value": event.get("old_value"),
                "new_value": event.get("new_value"),
                "source_message": event.get("source_message"),
            }
        )
    return {
        "ok": True,
        "user_id": user_id,
        "count": len(normalized_events),
        "changes": normalized_events,
    }


def _normalize_recent_change_detail(
    event: dict[str, Any],
    *,
    active_values: set[str] | None = None,
    suppressed_low_level_values: set[str] | None = None,
) -> str | None:
    action = str(event.get("action", ""))
    detail = str(event.get("detail", "")).strip()
    if not detail:
        return None
    if action == "reset_memory":
        return "Reset: cleared saved memory and started fresh."

    file_name = str(event.get("file") or "")
    old_file_name = str(event.get("old_file") or file_name)
    category = str(event.get("category") or _memory_change_label(file_name))
    old_value = str(event.get("old_value") or "").strip()
    new_value = str(event.get("new_value") or "").strip()
    active_values = active_values or set()
    suppressed_low_level_values = suppressed_low_level_values or set()

    if action == "trusted_memory_change":
        if old_value and new_value:
            if _normalize_timeline_value(old_value) == _normalize_timeline_value(new_value):
                return None
            return f"Updated {category}: {old_value} -> {new_value}"
        if new_value:
            return f"Added {category}: {new_value}"
        return None

    if action == "forget_memory":
        if old_value and _normalize_timeline_value(old_value) not in active_values:
            return f"Forgot {_memory_change_label(old_file_name)}: {old_value}"
        return None

    if action in {"update_file", "edit_file", "resolve_inbox"} and file_name == "CORRECTIONS.md":
        if detail.startswith("Added to Corrections: "):
            correction = detail.removeprefix("Added to Corrections: ").strip()
            normalized = normalize_correction_text(correction).rstrip(".") + "."
            if _normalize_timeline_value(normalized) in suppressed_low_level_values:
                return None
            return f"Added correction: {normalized}"
        if detail.startswith("Removed from Corrections: "):
            correction = detail.removeprefix("Removed from Corrections: ").strip()
            normalized = normalize_correction_text(correction).rstrip(".") + "."
            if _normalize_timeline_value(normalized) in active_values:
                return None
            return f"Removed correction: {normalized}"

    if action == "archive_old_fact":
        return None

    if action in {"update_file", "edit_file", "resolve_inbox"}:
        added_prefix = f"Added to {file_name.replace('.md', '').title()}: "
        removed_prefix = f"Removed from {file_name.replace('.md', '').title()}: "
        if detail.startswith(added_prefix):
            value = detail.removeprefix(added_prefix).strip()
            if _normalize_timeline_value(value) in suppressed_low_level_values:
                return None
            return f"Added {_memory_change_label(file_name)}: {value}"
        if detail.startswith(removed_prefix):
            value = detail.removeprefix(removed_prefix).strip()
            normalized_value = _normalize_timeline_value(value)
            if normalized_value in active_values or normalized_value in suppressed_low_level_values:
                return None
            return f"Removed {_memory_change_label(file_name)}: {value}"
    if ".md" in detail:
        return None
    return detail


def _memory_change_label(file_name: str) -> str:
    labels = {
        "IDENTITY.md": "identity",
        "BELIEFS.md": "belief",
        "PREFERENCES.md": "preference",
        "CORRECTIONS.md": "correction",
        "DECISIONS.md": "decision",
        "EVOLUTION.md": "timeline note",
    }
    return labels.get(file_name, "memory")


def _trusted_change_values(events: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for event in events:
        if event.get("action") != "trusted_memory_change":
            continue
        for key in ("old_value", "new_value"):
            value = str(event.get(key) or "").strip()
            if value:
                values.add(_normalize_timeline_value(value))
    return values


def build_structured_memory_summary(user_id: str | None = None) -> dict[str, object]:
    mem = get_memory_dir(user_id)
    sections = _read_sections_for_retrieval(mem)
    derived = read_active_derived_facts(mem)
    tensions = _read_structured_tensions(mem)
    active_facts = sum(len(items) for items in sections.values())

    highlights: list[str] = []
    if current_location := derived.get("person.location.current", []):
        highlights.append(f"Current location: {current_location[0]}")
    if previous_location := derived.get("person.location.previous", []):
        highlights.append(f"Previous location: {previous_location[0]}")
    if emoji_policy := derived.get("style.emoji_policy", []):
        value = emoji_policy[-1]
        highlights.append("Emoji policy: no emojis" if value == "deny" else "Emoji policy: emojis allowed")
    if detail_level := derived.get("style.detail_level", []):
        value = detail_level[-1]
        highlights.append("Default detail: concise" if value == "concise" else "Default detail: detailed")
    if output_format := derived.get("style.output_format", []):
        value = output_format[-1]
        highlights.append("Default format: bullets" if value == "bullets" else "Default format: long-form")

    trust_basis = _build_passport_basis(sections, derived, tensions)
    return {
        "active_fact_count": active_facts,
        "derived_fact_count": sum(len(items) for items in derived.values()),
        "semantic_index_enabled": sqlite_vec_available(),
        "highlights": highlights[:5],
        "trust_basis": trust_basis,
    }


def build_engram_context(
    user_id: str | None = None,
    *,
    issuer: str | None = None,
) -> dict[str, object]:
    """Build the v0.1 Engram context payload consumed by OpenClaw."""
    memory_user_id = _resolve_engram_memory_user_id(user_id)
    subject = user_id or os.getenv("EIGHTMEM_CONTEXT_SUBJECT", "local-user")
    sections = _read_sections_for_retrieval(get_memory_dir(memory_user_id))
    derived = read_active_derived_facts(get_memory_dir(memory_user_id))
    beliefs = _build_engram_beliefs(sections, derived)
    corrections = _build_engram_corrections(get_memory_dir(memory_user_id), beliefs)
    identity = _build_engram_identity(sections, derived, subject)
    issued_at = _engram_now(identity.get("timezone"))
    expires_at = issued_at + timedelta(days=1)
    agent_name = _resolve_agent_name()
    prompt = _build_system_prompt_injection(
        identity,
        sections,
        derived,
        corrections,
        beliefs,
        agent_name=agent_name,
        include_saved_memory=memory_user_id is not None,
    )

    return {
        "engram_version": "0.1",
        "issued_at": issued_at.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "issuer": issuer or os.getenv("EIGHTMEM_CONTEXT_ISSUER", "http://localhost:8787"),
        "subject": subject,
        "scope": "full",
        "identity": identity,
        "beliefs": beliefs,
        "corrections": corrections,
        "system_prompt_injection": prompt,
        "signature": {
            "kid": "key-1",
            "algorithm": "Ed25519",
            "value": "unsigned-v1",
        },
    }


def _engram_now(identity_timezone: str | None = None) -> datetime:
    timezone_name = os.getenv("EIGHTMEM_TIMEZONE") or identity_timezone
    if timezone_name:
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return datetime.now(timezone.utc).astimezone()


def _resolve_engram_memory_user_id(user_id: str | None) -> str | None:
    if user_id is None:
        return os.getenv("EIGHTMEM_CONTEXT_USER_ID") or None
    return user_id


def _build_engram_identity(
    sections: dict[str, list[str]],
    derived: dict[str, list[str]],
    subject: str,
) -> dict[str, str]:
    default_display_name = "User" if subject.isdigit() else ("Local user" if subject == "local-user" else subject)
    display_name = os.getenv("EIGHTMEM_CONTEXT_DISPLAY_NAME", default_display_name)
    timezone_name = os.getenv("EIGHTMEM_CONTEXT_TIMEZONE", os.getenv("TZ", "UTC"))
    if subject.isdigit() and not os.getenv("EIGHTMEM_CONTEXT_DISPLAY_NAME"):
        display_name, timezone_name = _default_identity_fallback(display_name, timezone_name)
    if timezone_values := derived.get("person.timezone.current", []):
        timezone_name = timezone_values[-1]
    for item in sections.get("IDENTITY.md", []):
        if item.lower().startswith("name:"):
            display_name = item.split(":", 1)[1].strip() or display_name
        if item.lower().startswith("timezone:"):
            timezone_name = item.split(":", 1)[1].strip() or timezone_name
    return {
        "display_name": display_name,
        "timezone": timezone_name,
        "language": os.getenv("EIGHTMEM_CONTEXT_LANGUAGE", "en"),
    }


def _default_identity_fallback(display_name: str, timezone_name: str) -> tuple[str, str]:
    try:
        default_sections = _read_sections_for_retrieval(get_memory_dir(None))
    except Exception:
        return display_name, timezone_name
    for item in default_sections.get("IDENTITY.md", []):
        if item.lower().startswith("name:"):
            display_name = item.split(":", 1)[1].strip() or display_name
        if item.lower().startswith("timezone:"):
            timezone_name = item.split(":", 1)[1].strip() or timezone_name
    return display_name, timezone_name


def _build_engram_beliefs(
    sections: dict[str, list[str]],
    derived: dict[str, list[str]],
) -> list[dict[str, object]]:
    beliefs: list[dict[str, object]] = []
    source_files = {
        "IDENTITY.md": ("identity", 0.82),
        "BELIEFS.md": ("belief", 0.78),
        "PREFERENCES.md": ("preference", 0.88),
        "CORRECTIONS.md": ("correction", 0.97),
        "DECISIONS.md": ("decision", 0.84),
        "EVOLUTION.md": ("evolution", 0.76),
    }
    for file_name in MEMORY_ORDER:
        category_source = source_files.get(file_name)
        if category_source is None:
            continue
        source, confidence = category_source
        for item in sections.get(file_name, []):
            category = infer_memory_category(item)
            beliefs.append(
                {
                    "id": _stable_engram_id("belief", file_name, item),
                    "category": category,
                    "key": _engram_key_for_memory(category, item),
                    "value": item.rstrip("."),
                    "confidence": confidence,
                    "source": source,
                    "status": "active",
                    "stale_after_days": 90,
                }
            )

    for key, values in sorted(derived.items()):
        for value in values:
            beliefs.append(
                {
                    "id": _stable_engram_id("derived", key, value),
                    "category": _engram_category_from_derived_key(key),
                    "key": key,
                    "value": value,
                    "confidence": 0.9,
                    "source": "derived",
                    "status": "active",
                    "stale_after_days": 90,
                }
            )
    return beliefs


def _build_engram_corrections(memory_dir: Path, beliefs: list[dict[str, object]]) -> list[dict[str, object]]:
    belief_by_value = {str(item.get("value", "")).lower(): str(item.get("id")) for item in beliefs}
    corrections: list[dict[str, object]] = []
    events = [
        event
        for event in _load_memory_events(memory_dir)
        if event.get("action") == "trusted_memory_change"
        and event.get("trigger") in {"correction", "contradiction"}
    ]
    for event in events[-12:]:
        new_value = str(event.get("new_value") or "").strip()
        old_value = str(event.get("old_value") or "").strip()
        if not new_value:
            continue
        if old_value and _normalize_timeline_value(old_value) == _normalize_timeline_value(new_value):
            continue
        belief_id = belief_by_value.get(new_value.rstrip(".").lower()) or _stable_engram_id("belief", "correction", new_value)
        corrections.append(
            {
                "id": _stable_engram_id("correction", str(event.get("ts", "")), old_value, new_value),
                "belief_id": belief_id,
                "old_value": old_value or None,
                "new_value": new_value,
                "corrected_at": str(event.get("ts") or ""),
                "method": "explicit" if event.get("trigger") == "correction" else "resolved_conflict",
            }
        )
    return corrections


def _build_system_prompt_injection(
    identity: dict[str, str],
    sections: dict[str, list[str]],
    derived: dict[str, list[str]],
    corrections: list[dict[str, object]],
    beliefs: list[dict[str, object]],
    *,
    agent_name: str,
    include_saved_memory: bool,
) -> str:
    display_name = identity.get("display_name") or "the user"
    timezone_name = identity.get("timezone") or "unknown timezone"
    style_facts: list[str] = []
    if emoji_policy := derived.get("style.emoji_policy", []):
        style_facts.append("No emoji." if emoji_policy[-1] == "deny" else "Emoji allowed.")
    if detail_level := derived.get("style.detail_level", []):
        style_facts.append("Prefers short, direct replies." if detail_level[-1] == "concise" else "Prefers detailed explanations.")
    if output_format := derived.get("style.output_format", []):
        style_facts.append("Prefers bullet structure." if output_format[-1] == "bullets" else "Comfortable with long-form replies.")
    for item in sections.get("PREFERENCES.md", []):
        if _skip_prompt_fact_due_to_derived_state(item, derived):
            continue
        normalized = _normalize_injected_style_fact(item)
        if normalized and normalized + "." not in style_facts and _is_style_or_preference_fact(normalized):
            style_facts.append(normalized + ".")
        if len(style_facts) >= 4:
            break
    for item in sections.get("BELIEFS.md", []):
        if _skip_prompt_fact_due_to_derived_state(item, derived):
            continue
        normalized = _normalize_injected_style_fact(item)
        if normalized and _is_style_or_preference_fact(normalized):
            _append_unique(style_facts, normalized + ".")
        if len(style_facts) >= 4:
            break

    saved_memory_facts: list[str] = []
    if include_saved_memory:
        for belief in beliefs:
            if belief.get("status") != "active":
                continue
            source = str(belief.get("source") or "")
            if source in {"derived", "evolution"}:
                continue
            value = str(belief.get("value") or "").strip()
            if _skip_prompt_fact_due_to_derived_state(value, derived):
                continue
            normalized = _normalize_injected_style_fact(value)
            if not normalized or not _is_user_facing_prompt_fact(normalized):
                continue
            sentence = normalized.rstrip(".") + "."
            if sentence in style_facts:
                continue
            _append_unique(saved_memory_facts, sentence)

    project_facts = _build_project_context_facts(sections, agent_name=agent_name)
    correction_facts: list[str] = []
    for item in sections.get("CORRECTIONS.md", []):
        if _skip_prompt_fact_due_to_derived_state(item, derived):
            continue
        normalized = _normalize_injected_style_fact(item)
        if normalized and _is_project_context_fact(normalized):
            _append_unique(correction_facts, normalized + ".")
        elif normalized and _is_style_or_preference_fact(normalized):
            _append_unique(style_facts, normalized + ".")
        if len(correction_facts) >= 3:
            break
    if not style_facts:
        style_facts.append("No strong saved communication preferences yet.")

    lines = [f"User is {display_name} ({timezone_name}).", " ".join(style_facts[:4])]
    if saved_memory_facts:
        lines.append("Saved memory:")
        lines.extend(f"- {item}" for item in saved_memory_facts)
    grounded_project_facts: list[str] = []
    for item in project_facts[:5] + correction_facts[:2]:
        _append_unique(grounded_project_facts, item)
    if grounded_project_facts:
        lines.append("Confirmed project context:")
        lines.extend(f"- {item}" for item in grounded_project_facts[:7])
        lines.append(
            "When explaining architecture, separate confirmed facts, planned work, and unknowns. "
            "Do not infer Engram is a vector database or suggest vector-store work unless explicitly asked."
        )
    prompt = "\n".join(line for line in lines if line.strip())
    return _limit_words(prompt, 900)


def _append_unique(items: list[str], item: str) -> None:
    normalized_item = _normalize_prompt_dedupe_key(item)
    if all(_normalize_prompt_dedupe_key(existing) != normalized_item for existing in items):
        items.append(item)


def _normalize_prompt_dedupe_key(value: str) -> str:
    return " ".join(value.lower().strip().rstrip(".").split())


def _normalize_injected_style_fact(value: str) -> str:
    normalized = value.strip().rstrip(".")
    lowered = normalized.lower()
    if lowered.startswith("prefer "):
        normalized = "Prefers " + normalized[7:]
    elif lowered.startswith("prefers "):
        normalized = "Prefers " + normalized[8:]
    elif lowered.startswith("keep "):
        normalized = "Prefers " + normalized[5:]
    if normalized.lower().startswith("prefers prefer "):
        normalized = "Prefers " + normalized[15:]
    return normalized


def _is_style_or_preference_fact(value: str) -> bool:
    lowered = value.lower()
    return bool(
        _memory_conflict_signals(value)
        or any(
            phrase in lowered
            for phrase in {
                "common language",
                "technical depth",
                "direct replies",
                "communication",
                "tone",
                "format",
            }
        )
    )


def _is_user_facing_prompt_fact(value: str) -> bool:
    lowered = value.lower()
    if _is_prompt_injection_artifact(value):
        return False
    if _is_internal_project_context_detail(value):
        return False
    if _is_project_context_fact(value):
        return False
    if any(
        phrase in lowered
        for phrase in {
            "api key",
            "bearer ",
            "token",
            "secret",
            "webhook",
            "localhost",
            "127.0.0.1",
            ".md",
            "sqlite",
            "jsonl",
        }
    ):
        return False
    return True


def _is_prompt_injection_artifact(value: str) -> bool:
    normalized = " ".join(value.strip().lower().split())
    return normalized.startswith(
        (
            "user is ",
            "timezone:",
            "language:",
            "name:",
            "the user's name is ",
        )
    )


def _build_project_context_facts(sections: dict[str, list[str]], *, agent_name: str) -> list[str]:
    corpus_items = [
        item
        for file_name in MEMORY_ORDER
        for item in sections.get(file_name, [])
        if _is_project_context_fact(item)
    ]
    corpus = " ".join(corpus_items).lower()
    facts: list[str] = []
    if "8mem" in corpus:
        facts.append("8mem is an AI memory layer that makes user memory visible, correctable, portable, and usable by AI runtimes.")
    if "engram" in corpus:
        facts.append("Engram v0.1 is an open JSON/context interchange spec for portable AI memory, not a vector database.")
    if "openclaw" in corpus:
        facts.append(f"{agent_name} uses 8mem memory so saved preferences and corrections carry across sessions.")
        if "write-back" in corpus or "write back" in corpus:
            facts.append(f"{agent_name} can save explicit remember, correct, and forget updates in 8mem.")
    for item in corpus_items:
        normalized = item.rstrip(".") + "."
        if _is_internal_project_context_detail(normalized):
            continue
        if len(facts) >= 7:
            break
        _append_unique(facts, normalized)
    return facts


def _resolve_agent_name() -> str:
    return os.getenv("EIGHTMEM_AGENT_NAME", "").strip() or "the connected agent"


def _is_project_context_fact(value: str) -> bool:
    lowered = value.lower()
    return any(
        phrase in lowered
        for phrase in {
            "8mem",
            "engram",
            "openclaw",
            "memory layer",
            "portable memory",
            "context interchange",
            "system_prompt_injection",
            "/v1/context",
            "heartbeat",
        }
    )


def _is_internal_project_context_detail(value: str) -> bool:
    lowered = value.lower()
    return any(
        phrase in lowered
        for phrase in {
            "heartbeat",
            "memory/8mem-context.md",
            "system_prompt_injection",
            "bearer ",
            "local_key",
            "api key",
            "port 8787",
            "agents.md",
            "write-back",
            "deleted memory from",
        }
    )


def _skip_prompt_fact_due_to_derived_state(item: str, derived: dict[str, list[str]]) -> bool:
    signals = _memory_conflict_signals(item)
    if detail_level := derived.get("style.detail_level", []):
        active_detail = detail_level[-1]
        if active_detail == "concise" and "detailed" in signals:
            return True
        if active_detail == "detailed" and "concise" in signals:
            return True
    if emoji_policy := derived.get("style.emoji_policy", []):
        active_emoji = emoji_policy[-1]
        if active_emoji == "deny" and "emoji_allow" in signals:
            return True
        if active_emoji == "allow" and "emoji_deny" in signals:
            return True
    if output_format := derived.get("style.output_format", []):
        active_format = output_format[-1]
        if active_format == "bullets" and "long_form" in signals:
            return True
        if active_format == "long_form" and "bullets" in signals:
            return True
    return False


def _engram_key_for_memory(category: str, value: str) -> str:
    signals = _memory_conflict_signals(value)
    if "emoji_deny" in signals or "emoji_allow" in signals:
        return "emoji_policy"
    if "concise" in signals or "detailed" in signals:
        return "reply_length"
    if "bullets" in signals or "long_form" in signals:
        return "format"
    if "morning" in signals or "late_night" in signals:
        return "work_timing"
    if category == "location":
        return "current_location"
    if category == "timezone":
        return "timezone"
    return re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") or "memory"


def _engram_category_from_derived_key(key: str) -> str:
    if key.startswith("style."):
        return "communication"
    if key.startswith("person.location"):
        return "location"
    if key.startswith("person.timezone"):
        return "timezone"
    return "memory"


def _stable_engram_id(*parts: str) -> str:
    raw = "|".join(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"8mem:engram:{raw}"))


def _limit_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(" .,") + "."


def build_compare_output(
    user_id: str | None,
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    draft_type = _classify_draft_request(prompt)
    if draft_type == "generic":
        without_memory = generate_generic_reply(
            prompt,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        with_memory = generate_memory_aware_reply(
            user_id,
            prompt,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
    else:
        without_memory = generate_generic_draft(
            prompt,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        with_memory = generate_memory_aware_draft(
            user_id,
            prompt,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    basis = _build_compare_memory_basis(user_id, prompt)
    weak_signal = _is_weak_compare_signal(without_memory, with_memory, basis)
    comparison = (
        "8mem Compare\n"
        f"Prompt: {prompt.strip()}\n\n"
        "Standard answer:\n"
        f"{without_memory}\n\n"
        "Answer shaped by what I know about you:\n"
        f"{with_memory}"
    )
    if basis:
        comparison += "\n\nWhat changed because of memory:\n" + "\n".join(f"- {item}" for item in basis[:4])
    if weak_signal:
        comparison += (
            "\n\n"
            "Memory signal is weak for this prompt.\n"
            "I don't have enough style or project signal yet to make this meaningfully different.\n"
            "Add a clearer preference, correction, or project fact to make this more useful."
        )
    return comparison


def _normalize_compare_output(text: str) -> str:
    return " ".join(text.lower().split())


def _build_compare_memory_basis(user_id: str | None, prompt: str = "") -> list[str]:
    sections = get_retrieval_sections(user_id)
    derived = read_active_derived_facts(get_memory_dir(user_id))
    basis: list[str] = []

    if detail_level := derived.get("style.detail_level", []):
        basis.append("Reply length/style: concise" if detail_level[-1] == "concise" else "Reply length/style: detailed")
    if output_format := derived.get("style.output_format", []):
        basis.append("Format: structured bullets" if output_format[-1] == "bullets" else "Format: long-form")
    if emoji_policy := derived.get("style.emoji_policy", []):
        basis.append("Emoji policy: no emojis" if emoji_policy[-1] == "deny" else "Emoji policy: emojis allowed")

    if prompt.strip():
        for item in _build_semantic_compare_memory_basis(user_id, prompt):
            _append_unique(basis, item)

    for item in sections.get("BELIEFS.md", []):
        normalized = item.rstrip(".")
        if _is_project_context_fact(normalized):
            _append_unique(basis, f"Project context: {normalized}")
        elif _is_style_or_preference_fact(normalized):
            _append_unique(basis, f"Preference: {normalized}")
        else:
            _append_unique(basis, f"Project context: {normalized}")

    for item in sections.get("PREFERENCES.md", []):
        normalized = item.rstrip(".")
        if _is_project_context_fact(normalized):
            _append_unique(basis, f"Project context: {normalized}")
        elif _is_style_or_preference_fact(normalized):
            _append_unique(basis, f"Preference: {normalized}")

    for item in sections.get("CORRECTIONS.md", []):
        normalized = item.rstrip(".")
        if _is_project_context_fact(normalized):
            _append_unique(basis, f"Project guardrail: {normalized}")
        elif _is_style_or_preference_fact(normalized):
            _append_unique(basis, f"Correction: {normalized}")

    return _rank_compare_memory_basis(basis)[:4]


def _build_semantic_compare_memory_basis(user_id: str | None, prompt: str) -> list[str]:
    matches = search_semantic_memory(get_memory_dir(user_id), prompt, k=4)
    basis: list[str] = []
    for match in matches:
        text = str(match.get("text") or "").strip().rstrip(".")
        source_key = str(match.get("source_key") or "")
        if not text:
            continue
        formatted = _format_compare_basis_item(source_key, text)
        if formatted:
            _append_unique(basis, formatted)
    return basis


def _format_compare_basis_item(source_key: str, text: str) -> str:
    file_name = source_key.split(":", 1)[0]
    if file_name == "PREFERENCES.md":
        return f"Preference: {text}"
    if file_name == "CORRECTIONS.md":
        return f"Correction: {text}"
    if file_name == "BELIEFS.md":
        label = "Project context" if _is_project_context_fact(text) else "Memory"
        return f"{label}: {text}"
    if file_name == "IDENTITY.md":
        return f"Identity: {text}"
    if file_name == "DECISIONS.md":
        return f"Decision: {text}"
    return f"Memory: {text}"


def _rank_compare_memory_basis(basis: list[str]) -> list[str]:
    ranked = sorted(enumerate(basis), key=lambda item: (-_compare_basis_priority(item[1]), item[0]))
    return [item for _, item in ranked]


def _compare_basis_priority(item: str) -> int:
    normalized = item.lower()
    score = 0
    if normalized.startswith(("reply length/style:", "format:")):
        score += 30
    if normalized.startswith(("preference:", "correction:", "project guardrail:")):
        score += 20
    if normalized.startswith("project context:"):
        score += 12
    if any(marker in normalized for marker in {"execution speed", "stability", "investor", "founder", "control", "dilution", "ownership"}):
        score += 25
    if "8mem, an ai memory layer" in normalized:
        score -= 10
    if "emoji policy" in normalized:
        score -= 5
    return score


def _is_weak_compare_signal(without_memory: str, with_memory: str, basis: list[str]) -> bool:
    if _normalize_compare_output(without_memory) == _normalize_compare_output(with_memory):
        return True
    if not basis:
        return True
    emoji_only = basis and all("emoji policy" in item.lower() or "emoji" in item.lower() for item in basis)
    return emoji_only or not _has_share_card_ready_basis(basis)


def _has_share_card_ready_basis(basis: list[str]) -> bool:
    for item in basis:
        normalized = item.lower()
        if "project context:" in normalized or "project guardrail:" in normalized:
            return True
        if "reply length/style: concise" in normalized:
            return True
        if "format: structured bullets" in normalized:
            return True
        if any(
            marker in normalized
            for marker in {
                "progress / risk / next",
                "progress/risk/next",
                "structured",
                "short",
                "direct",
                "concise",
                "execution speed",
                "stability",
                "generic motivational",
                "founder",
                "investor",
            }
        ):
            return True
    return False


def normalize_preference_text(text: str) -> str:
    clause = _normalize_clause(text)
    lower = clause.lower()
    if lower.startswith("values "):
        remainder = clause[7:].strip()
        if remainder:
            return f"Values {remainder}."
    if lower.startswith("wants "):
        remainder = clause[6:].strip()
        if remainder:
            return f"Prefers {remainder}."
    if lower.startswith("prefers values "):
        return normalize_preference_text(clause[8:].strip())
    if lower.startswith("prefers wants "):
        return normalize_preference_text(clause[8:].strip())
    if lower.startswith("prefers "):
        return normalize_preference_text(clause[8:].strip())
    if lower.startswith("i prefer "):
        remainder = clause[9:].strip()
        if remainder:
            return f"Prefers {remainder}."
    if lower.startswith("i like "):
        remainder = clause[7:].strip()
        if remainder:
            return f"Prefers {remainder}."
    if lower.startswith("i want "):
        remainder = clause[7:].strip()
        if remainder:
            return f"Prefers {remainder}."
    if lower.startswith("i value "):
        remainder = clause[8:].strip()
        if remainder:
            return f"Values {remainder}."
    if lower.startswith("i care about "):
        remainder = clause[13:].strip()
        if remainder:
            return f"Values {remainder}."
    if lower.startswith("i live in "):
        remainder = clause[10:].strip()
        if remainder:
            return f"Current location: {remainder}."
    if lower.startswith("i am based in "):
        remainder = clause[14:].strip()
        if remainder:
            return f"Current location: {remainder}."
    if lower.startswith("my timezone is "):
        return f"Timezone {clause[15:].strip()}."
    if lower.startswith("current location:"):
        return clause.rstrip(".") + "."
    if lower.startswith("timezone "):
        remainder = clause[9:].strip(" .")
        return f"Timezone {remainder}." if remainder else clause
    if lower.startswith("keep "):
        remainder = clause[5:].strip()
        if remainder:
            return f"Keep {remainder}."
    if lower.startswith("give "):
        remainder = clause[5:].strip()
        if remainder:
            return f"Give {remainder}."
    if lower.startswith("use "):
        remainder = clause[4:].strip()
        if remainder:
            return f"Use {remainder}."
    if lower.startswith("write in "):
        remainder = clause[9:].strip()
        if remainder:
            return f"Write in {remainder}."
    if lower.startswith("respond in "):
        remainder = clause[10:].strip()
        if remainder:
            return f"Respond in {remainder}."
    if lower.startswith("avoid "):
        remainder = clause[6:].strip()
        if remainder:
            return f"Avoid {remainder}."
    rendered = _render_directive_preference(text)
    return rendered or text.strip()


def normalize_correction_text(text: str) -> str:
    clause = _normalize_clause(text)
    lowered = clause.lower()
    if lowered.startswith("that's wrong, "):
        clause = clause[14:].strip()
        lowered = clause.lower()
    if lowered.startswith("that is wrong, "):
        clause = clause[15:].strip()
        lowered = clause.lower()
    if lowered.startswith("wrong, "):
        clause = clause[7:].strip()
        lowered = clause.lower()
    if lowered.startswith("use "):
        return f"Use {clause[4:].strip()}."
    return clause or text.strip()


def reset_memory_runtime(user_id: str | None = None) -> Path:
    mem = get_memory_dir(user_id)
    before_files = {
        name: (mem / name).read_text(encoding="utf-8") if (mem / name).exists() else _render(name, [])
        for name in MEMORY_ORDER
    }
    before_counts = {
        name: len(_parse_bullets((mem / name).read_text(encoding="utf-8"))) if (mem / name).exists() else 0
        for name in MEMORY_ORDER
    }
    source_dir = template_dir()
    for name in MEMORY_ORDER:
        src = source_dir / name
        dst = mem / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    cleared = sum(before_counts.values())
    _sync_memory_dir_state(mem)
    after_files = {name: (mem / name).read_text(encoding="utf-8") for name in MEMORY_ORDER}
    _record_memory_event(
        mem,
        action="reset_memory",
        detail=f"Reset memory for this user and cleared {cleared} saved entries.",
        before_files=before_files,
        after_files=after_files,
    )
    return mem


def _canonicalize_existing_entries(memory_dir: Path, changed_names: object) -> None:
    changed = set(changed_names)
    for name in {"IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md"} & changed:
        path = memory_dir / name
        if not path.exists():
            continue
        existing = _parse_bullets(path.read_text(encoding="utf-8"))
        normalized = []
        for item in existing:
            normalized.extend(_normalize_existing_entry(name, item))
        # Preserve stable order while removing duplicates after normalization.
        deduped = list(dict.fromkeys(item for item in normalized if item))
        if deduped != existing:
            path.write_text(_render(name, deduped), encoding="utf-8")


def _normalize_existing_entry(name: str, item: str) -> list[str]:
    if name == "IDENTITY.md":
        normalized = _normalize_identity_entry(item)
        return [normalized] if normalized else []
    if name == "BELIEFS.md":
        normalized = _normalize_belief_entry(item)
        return [normalized] if normalized else []
    if name == "PREFERENCES.md":
        return [normalize_preference_text(item)]
    if name == "CORRECTIONS.md":
        extracted = list(dict.fromkeys(_extract_corrections(item)))
        if extracted:
            return [normalize_correction_text(entry) for entry in extracted]
        return [normalize_correction_text(item)]
    return [item]


def _normalize_identity_entry(item: str) -> str:
    clause = _normalize_clause(item)
    lowered = clause.lower()
    if " and i am " in lowered:
        clause = clause[lowered.index(" and i am ") + len(" and i am ") :].strip()
    elif lowered.startswith("i am "):
        clause = clause[5:].strip()
    elif lowered.startswith("i'm "):
        clause = clause[4:].strip()
    return clause


def _normalize_belief_entry(item: str) -> str:
    clause = _normalize_clause(item)
    lowered = clause.lower()
    if lowered.startswith("possible focus area:"):
        return ""
    if lowered.startswith("likely recurring theme: prefer"):
        return ""
    return clause


def _build_memory_health(sections: dict[str, list[str]], tensions: list[str]) -> dict[str, object]:
    entry_count = sum(len(items) for items in sections.values())
    corrections_count = len(sections.get("CORRECTIONS.md", []))
    preferences_count = len(sections.get("PREFERENCES.md", []))

    if entry_count == 0:
        status = "empty"
        label = "No trusted memory yet"
    elif tensions:
        status = "attention"
        label = "Needs review"
    else:
        status = "stable"
        label = "Stable"

    signals: list[str] = []
    if entry_count == 0:
        signals.append("No saved memory yet. Add a preference or correction to start shaping replies.")
    else:
        signals.append(f"{entry_count} active memory entries are available across the core files.")
        if preferences_count:
            signals.append(f"{preferences_count} preference entries are shaping response style and format.")
        if corrections_count:
            signals.append(f"{corrections_count} correction entries are acting as hard guidance.")
        if tensions:
            signals.append(f"{len(tensions)} contradiction signal(s) need review before the memory can be fully trusted.")
        else:
            signals.append("No contradiction signals detected in the current memory.")

    return {
        "status": status,
        "label": label,
        "signals": signals[:4],
        "tension_count": len(tensions),
        "entry_count": entry_count,
    }


def _build_resolution_suggestions(tensions: list[str], *, user_id: str | None = None) -> list[dict[str, str]]:
    if not tensions:
        return []

    suffix = f"?user_id={user_id}" if user_id else ""
    suggestions: list[dict[str, str]] = []

    for tension in tensions:
        lower = tension.lower()
        if "emoji guidance" in lower:
            suggestions.append(
                {
                    "id": "emoji_guidance",
                    "title": "Resolve emoji guidance",
                    "reason": "Your memory says both to use emojis and not to use emojis.",
                    "action_label": "Edit corrections",
                    "href": f"/files/CORRECTIONS.md{suffix}",
                    "note": "Keep only the guidance you want the assistant to follow by default.",
                }
            )
            continue
        if "structured bullets versus long detailed explanations" in lower:
            suggestions.append(
                {
                    "id": "format_conflict",
                    "title": "Choose a default writing format",
                    "reason": "The memory currently asks for both structured bullets and long detailed explanations.",
                    "action_label": "Edit preferences",
                    "href": f"/files/PREFERENCES.md{suffix}",
                    "note": "You can keep one as the default or rewrite both to clarify when each should apply.",
                }
            )
            continue
        if "concise answers versus detailed explanations" in lower or "long-form guidance" in lower:
            suggestions.append(
                {
                    "id": "detail_conflict",
                    "title": "Clarify concise vs detailed",
                    "reason": "The memory contains both concise and detailed response guidance.",
                    "action_label": "Review preferences",
                    "href": f"/files/PREFERENCES.md{suffix}",
                    "note": "Rewrite the preference to say when to be concise and when more detail is appropriate.",
                }
            )
            continue
        suggestions.append(
            {
                "id": "generic_tension",
                "title": "Review memory tension",
                "reason": tension,
                "action_label": "Open memory files",
                "href": f"/files{suffix}",
                "note": "Review the conflicting entries and keep the clearest default rule.",
            }
        )

    return suggestions[:3]


def _event_log_path(memory_dir: Path) -> Path:
    return memory_dir / EVENT_LOG_NAME


def _record_memory_event(
    memory_dir: Path,
    *,
    action: str,
    file_name: str | None = None,
    detail: str,
    before: list[str] | None = None,
    after: list[str] | None = None,
    before_files: dict[str, str] | None = None,
    after_files: dict[str, str] | None = None,
    trigger: str | None = None,
    category: str | None = None,
    old_value: str | None = None,
    old_file: str | None = None,
    new_value: str | None = None,
    new_file: str | None = None,
    source_message: str | None = None,
    status: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    event = {
        "ts": ts,
        "action": action,
        "file": file_name,
        "detail": detail,
    }
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    if before_files is not None:
        event["before_files"] = before_files
    if after_files is not None:
        event["after_files"] = after_files
    for key, value in {
        "trigger": trigger,
        "category": category,
        "old_value": old_value,
        "old_file": old_file,
        "new_value": new_value,
        "new_file": new_file,
        "source_message": source_message,
        "status": status,
    }.items():
        if value is not None:
            event[key] = value
    path = _event_log_path(memory_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")
    append_memory_event_to_sqlite(
        memory_dir,
        ts=ts,
        action=action,
        file_name=file_name,
        detail=detail,
        payload=event,
    )


def _record_diff_event(
    memory_dir: Path,
    *,
    action: str,
    file_name: str,
    before: list[str],
    after: list[str],
) -> None:
    added = [item for item in after if item not in before]
    removed = [item for item in before if item not in after]
    if not added and not removed:
        return

    file_label = file_name.replace(".md", "").title()
    if len(added) == 1 and not removed:
        detail = f"Added to {file_label}: {added[0]}"
    elif len(removed) == 1 and not added:
        detail = f"Removed from {file_label}: {removed[0]}"
    else:
        detail = f"Updated {file_label}: +{len(added)} / -{len(removed)} entries."

    _record_memory_event(memory_dir, action=action, file_name=file_name, detail=detail, before=before, after=after)


def _remove_exact_memory_entry(memory_dir: Path, file_name: str, value: str) -> bool:
    path = memory_dir / file_name
    if not path.exists():
        return False
    before = _parse_bullets(path.read_text(encoding="utf-8"))
    after = [item for item in before if item != value]
    if after == before:
        return False
    path.write_text(_render(file_name, after), encoding="utf-8")
    sync_memory_file_to_sqlite(memory_dir, file_name, after)
    _record_diff_event(memory_dir, action="archive_old_fact", file_name=file_name, before=before, after=after)
    return True


def _format_trusted_change_detail(event: dict[str, str | None]) -> str:
    category = event.get("category") or "memory"
    new_value = str(event.get("new_value") or "").strip()
    old_value = event.get("old_value")
    if old_value:
        return f'Corrected {category}: "{old_value}" -> "{new_value}"'
    return f'Added correction to {category}: "{new_value}"'


def _format_event_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Unknown"
    return f"{parsed.strftime('%b')} {parsed.day}"


def _memory_conflict_signals(value: str) -> set[str]:
    lower = value.lower()
    signals: set[str] = set()
    if "emoji" in lower:
        if any(phrase in lower for phrase in {"do not use emoji", "don't use emoji", "avoid emoji", "no emoji", "won't use emoji"}):
            signals.add("emoji_deny")
        elif any(phrase in lower for phrase in {"use emoji", "allow emoji", "emojis allowed"}):
            signals.add("emoji_allow")
    if any(phrase in lower for phrase in {"concise", "short answer", "short answers", "short replies", "short and direct", "brief"}):
        signals.add("concise")
    if any(phrase in lower for phrase in {"detailed explanation", "detailed answers", "long explanation", "long detailed", "verbose"}):
        signals.add("detailed")
    if any(phrase in lower for phrase in {"bullet", "structured"}):
        signals.add("bullets")
    if any(phrase in lower for phrase in {"long-form", "long form", "long narrative", "paragraphs"}):
        signals.add("long_form")
    if "dark mode" in lower or "dark theme" in lower:
        signals.add("dark_theme")
    if "light mode" in lower or "light theme" in lower:
        signals.add("light_theme")
    if any(phrase in lower for phrase in {"morning", "6am", "6 am", "early work"}):
        signals.add("morning")
    if any(phrase in lower for phrase in {"late night", "night work", "night deep work", "after midnight"}):
        signals.add("late_night")
    if "timezone" in lower or "utc" in lower:
        if timezone_value := _extract_timezone_signal(lower):
            signals.add(f"timezone:{timezone_value}")
        else:
            signals.add("timezone")
    if any(phrase in lower for phrase in {"live in", "moved to", "current location", "based in"}):
        if location_value := _extract_location_signal(value):
            signals.add(f"location:{location_value}")
        else:
            signals.add("location")
    if color_value := _extract_personal_attribute_signal(value, ("favourite colour", "favorite color", "favourite color", "favorite colour")):
        signals.add(f"favorite_color:{color_value}")
    if drink_value := _extract_personal_attribute_signal(
        value,
        ("favourite drink", "favorite drink", "favourite drinks", "favorite drinks"),
    ):
        signals.add(f"favorite_drink:{drink_value}")
    if tool_value := _extract_default_tool_signal(lower):
        signals.add(f"default_tool:{tool_value}")
    return signals


def _memory_values_conflict(left: set[str], right: set[str]) -> bool:
    conflict_pairs = {
        frozenset({"emoji_allow", "emoji_deny"}),
        frozenset({"concise", "detailed"}),
        frozenset({"bullets", "long_form"}),
        frozenset({"bullets", "detailed"}),
        frozenset({"morning", "late_night"}),
        frozenset({"dark_theme", "light_theme"}),
    }
    for pair in conflict_pairs:
        if pair.issubset(left | right) and left & pair and right & pair:
            return True
    for prefix in {"location:", "timezone:", "default_tool:", "favorite_color:", "favorite_drink:"}:
        if _tag_values_conflict(left, right, prefix):
            return True
    return False


def _tag_values_conflict(left: set[str], right: set[str], prefix: str) -> bool:
    left_values = {item.removeprefix(prefix) for item in left if item.startswith(prefix)}
    right_values = {item.removeprefix(prefix) for item in right if item.startswith(prefix)}
    return bool(left_values and right_values and left_values.isdisjoint(right_values))


def _extract_location_signal(value: str) -> str | None:
    patterns = [
        r"\bmoved to\s+([A-Za-z][A-Za-z\s]+?)\s+from\s+[A-Za-z]",
        r"\bcurrently live in\s+([A-Za-z][A-Za-z\s]+)",
        r"\blive in\s+([A-Za-z][A-Za-z\s]+)",
        r"\bbased in\s+([A-Za-z][A-Za-z\s]+)",
        r"\bcurrent location\s*(?:is|:)?\s*([A-Za-z][A-Za-z\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(1).strip(" .").lower().split())
    return None


def _extract_personal_attribute_signal(value: str, labels: tuple[str, ...]) -> str | None:
    normalized = " ".join(value.lower().replace("colour", "color").split())
    normalized_labels = tuple(label.replace("colour", "color") for label in labels)
    label_pattern = "|".join(re.escape(label) for label in normalized_labels)
    patterns = [
        rf"\b(?:my\s+)?(?:{label_pattern})\s*(?:is|:|=)\s*([a-z][a-z\s-]+)",
        rf"\b(?:set|keep|remember)\s+(?:my\s+)?(?:{label_pattern})\s+(?:as|to)\s+([a-z][a-z\s-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        candidate = re.split(r"[.;,!?]|\s+(?:and|but|because|from|to)\s+", match.group(1).strip(), maxsplit=1)[0].strip()
        if candidate:
            return re.sub(r"\s+", " ", candidate)
    return None


def _extract_timezone_signal(lower: str) -> str | None:
    if match := re.search(r"\butc\s*([+-]\s*\d{1,2})(?::?(\d{2}))?\b", lower):
        hours = match.group(1).replace(" ", "")
        minutes = match.group(2) or "00"
        return f"utc{hours}:{minutes}"
    if match := re.search(r"\btimezone\s*(?:is|:)?\s*([a-z]+/[a-z_]+)\b", lower):
        return match.group(1)
    return None


def _extract_default_tool_signal(lower: str) -> str | None:
    if not any(phrase in lower for phrase in {"by default", "default tool", "default language", "prefer using"}):
        return None
    for tool in {"python", "typescript", "javascript", "node", "react", "go", "rust"}:
        if re.search(rf"\b{re.escape(tool)}\b", lower):
            return tool
    return None


def _load_memory_events(memory_dir: Path) -> list[dict[str, Any]]:
    path = _event_log_path(memory_dir)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            events.append(raw)
    return events


def _build_memory_timeline(memory_dir: Path, sections: dict[str, list[str]]) -> list[str]:
    events = _load_memory_events(memory_dir)
    if events:
        active_values = _active_timeline_values(sections)
        timeline: list[str] = []
        for event in reversed(events):
            detail = _dashboard_timeline_detail(event, active_values)
            if detail:
                timeline.append(detail)
            if len(timeline) >= 5:
                break
        return timeline or ["Memory timeline will appear after the first saved changes."]

    evolution = sections.get("EVOLUTION.md", [])
    summary: list[str] = []
    preference_count = len(sections.get("PREFERENCES.md", []))
    correction_count = len(sections.get("CORRECTIONS.md", []))
    identity_count = len(sections.get("IDENTITY.md", []))
    if preference_count or correction_count or identity_count:
        summary.append(
            f"Current memory snapshot: {identity_count} identity, {preference_count} preferences, and {correction_count} corrections are active."
        )
    if evolution:
        summary.extend(f"Latest evolution note: {item}" for item in evolution[-2:])
    return summary or ["Memory timeline will appear after the first saved changes."]


def _active_timeline_values(sections: dict[str, list[str]]) -> set[str]:
    values: set[str] = set()
    for file_name in ("IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md", "DECISIONS.md"):
        for item in sections.get(file_name, []):
            normalized = _normalize_timeline_value(item)
            if normalized:
                values.add(normalized)
    return values


def _dashboard_timeline_detail(event: dict[str, Any], active_values: set[str]) -> str | None:
    detail = str(event.get("detail") or "").strip()
    if not detail:
        return None

    action = str(event.get("action") or "")
    old_value = str(event.get("old_value") or "").strip()
    if action == "forget_memory" and _normalize_timeline_value(old_value) in active_values:
        return None

    if detail.startswith("Deleted memory from "):
        if not old_value or _normalize_timeline_value(old_value) in active_values:
            return None
        if _contains_internal_runtime_jargon(old_value):
            return "Forgot an internal integration detail."
        return f"Forgot: {old_value}"
    if _contains_internal_runtime_jargon(detail):
        return "Memory updated and synced across connected AI agents."
    return detail


def _normalize_timeline_value(value: str) -> str:
    return " ".join(value.strip().strip('"').rstrip(".").lower().split())


def _find_last_reversible_event(memory_dir: Path) -> dict[str, Any] | None:
    events = _load_memory_events(memory_dir)
    for event in reversed(events):
        action = str(event.get("action", ""))
        if action in {"edit_file", "update_file", "resolve_inbox"}:
            if isinstance(event.get("file"), str) and isinstance(event.get("before"), list):
                return event
        if action == "reset_memory" and isinstance(event.get("before_files"), dict):
            return event
    return None


def _remove_entries_matching(memory_dir: Path, file_name: str, needle: str) -> bool:
    return _remove_entries_matching_any(memory_dir, file_name, [needle])


def _remove_entries_matching_any(memory_dir: Path, file_name: str, needles: list[str]) -> bool:
    path = memory_dir / file_name
    if not path.exists():
        return False
    before = _parse_bullets(path.read_text(encoding="utf-8"))
    lowered_needles = [needle.lower() for needle in needles]
    after = [
        item
        for item in before
        if not any(needle in item.lower() for needle in lowered_needles)
    ]
    if after == before:
        return False
    path.write_text(_render(file_name, after), encoding="utf-8")
    sync_memory_file_to_sqlite(memory_dir, file_name, after)
    _record_diff_event(memory_dir, action="resolve_inbox", file_name=file_name, before=before, after=after)
    return True


def _read_sections_for_retrieval(memory_dir: Path) -> dict[str, list[str]]:
    if sqlite_has_active_facts(memory_dir):
        return read_active_sections_from_sqlite(memory_dir)
    return {
        name: _parse_bullets((memory_dir / name).read_text(encoding="utf-8")) if (memory_dir / name).exists() else []
        for name in MEMORY_ORDER
    }


def _read_structured_tensions(memory_dir: Path) -> list[str]:
    if sqlite_has_active_facts(memory_dir):
        derived = read_active_derived_facts(memory_dir)
        tensions: list[str] = []
        emoji_values = set(derived.get("style.emoji_policy", []))
        detail_values = set(derived.get("style.detail_level", []))
        format_values = set(derived.get("style.output_format", []))
        if {"allow", "deny"} <= emoji_values:
            tensions.append("Emoji guidance appears inconsistent across notes.")
        if {"concise", "detailed"} <= detail_values:
            tensions.append("Preference guidance conflicts: concise answers versus detailed explanations.")
        if {"bullets", "long_form"} <= format_values:
            tensions.append("Preference guidance conflicts: structured bullets versus long detailed explanations.")
        return tensions

    preferences = _parse_bullets((memory_dir / "PREFERENCES.md").read_text(encoding="utf-8")) if (memory_dir / "PREFERENCES.md").exists() else []
    corrections = _parse_bullets((memory_dir / "CORRECTIONS.md").read_text(encoding="utf-8")) if (memory_dir / "CORRECTIONS.md").exists() else []
    return _detect_tensions(
        "\n".join(f"- {item}" for item in preferences),
        "\n".join(f"- {item}" for item in corrections),
    )


def _build_passport_basis(
    sections: dict[str, list[str]],
    derived: dict[str, list[str]],
    tensions: list[str],
) -> list[str]:
    basis: list[str] = []
    preference_count = len(sections.get("PREFERENCES.md", []))
    correction_count = len(sections.get("CORRECTIONS.md", []))
    if preference_count:
        basis.append(f"I'm using {preference_count} saved preference{'s' if preference_count != 1 else ''}.")
    if correction_count:
        basis.append(f"I'm also following {correction_count} saved correction{'s' if correction_count != 1 else ''}.")
    if derived.get("person.location.current"):
        basis.append("Current location comes from your latest saved update.")
    if tensions:
        basis.append("Some saved notes still conflict, so this memory needs review.")
    else:
        basis.append("Nothing in your saved memory conflicts right now.")
    return basis[:4]


def _build_semantic_context_block(memory_dir: Path, prompt: str) -> str:
    matches = search_semantic_memory(memory_dir, prompt, k=3)
    if not matches:
        return ""
    lines = ["## Related memories"]
    for item in matches:
        lines.append(f"- {item['text']}")
    lines.append("")
    return "\n".join(lines)


def _sync_memory_dir_state(memory_dir: Path) -> None:
    files = {
        name: _parse_bullets((memory_dir / name).read_text(encoding="utf-8")) if (memory_dir / name).exists() else []
        for name in MEMORY_ORDER
    }
    sync_memory_dir_to_sqlite(memory_dir, files)


def _clean_draft_response(text: str, *, draft_type: str = "generic", user_id: str | None = None) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return text.strip()

    first = lines[0].strip().lower()
    if first.startswith("here is ") or first.startswith("here's ") or first.startswith("below is "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    if lines and (lines[0].startswith("# ") or lines[0].startswith("## ")):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    while lines and lines[0].strip().lower().startswith("based on the provided memory context"):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    while lines and (
        lines[0].strip().lower().startswith("here's a draft")
        or lines[0].strip().lower().startswith("here is a draft")
        or lines[0].strip().lower().startswith("draft:")
    ):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    cleaned = "\n".join(lines).strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) > 1:
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned or text.strip()
    return _enforce_draft_shape(cleaned, draft_type=draft_type, user_id=user_id)


def _build_draft_hints(user_id: str | None, prompt: str, draft_type: str) -> str:
    mem = get_memory_dir(user_id)
    preferences = _parse_bullets((mem / "PREFERENCES.md").read_text(encoding="utf-8"))
    corrections = _parse_bullets((mem / "CORRECTIONS.md").read_text(encoding="utf-8"))
    lowered_prompt = prompt.lower()

    hints: list[str] = []
    if any("structured" in item.lower() or "bullet" in item.lower() for item in preferences):
        hints.append("Format preference: prefer a structured layout or bullets when appropriate.")
    if any("owner" in item.lower() or "deadline" in item.lower() for item in preferences):
        hints.append("If the request is about execution or planning, include clear owners and deadlines when the prompt allows it.")
    if draft_type == "weekly_update" or ("weekly" in lowered_prompt and "update" in lowered_prompt):
        hints.append("For a weekly update, prefer compact bullets covering progress, risks, and next steps.")
    if draft_type == "founder_update":
        hints.append("For a founder update, sound like an operator giving a concise status update, not a motivational speech.")
    if draft_type == "product_recommendation":
        hints.append("For a product recommendation, give a recommendation first, then tradeoffs and explicit risks.")
    if draft_type == "summarize":
        hints.append("For a summary, preserve the user's preferred structure, brevity, and tone.")
    if any("do not assume i want long strategic essays" in item.lower() for item in corrections):
        hints.append("Keep the output compact and practical; avoid strategic-essay tone.")
    if any("do not give generic motivational language" in item.lower() for item in corrections):
        hints.append("Avoid generic motivational language, slogans, and vague executive phrasing.")
    if any("do not hide uncertainty" in item.lower() for item in corrections):
        hints.append("State uncertainty openly instead of hiding it.")
    if not hints:
        return ""
    return "\n".join(hints) + "\n\n"


def _build_style_control_instructions(user_id: str | None, prompt: str, *, draft_type: str) -> str:
    controls = _memory_style_controls(user_id)
    lowered_prompt = prompt.lower()
    lines: list[str] = []

    if controls["concise"]:
        lines.append("Style control: keep the output to at most 2 short sentences and under 24 words unless bullets are clearly required.")
    if controls["no_emojis"]:
        lines.append("Style control: do not use emojis.")
    if draft_type == "generic" or "client" in lowered_prompt or "reply" in lowered_prompt:
        lines.append("Style control: write exactly as a message the user would send, not as an assistant commentary.")
        lines.append("Avoid phrases like 'I received your message', 'I'd be happy to', and 'I'll adjust my calendar accordingly'.")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _classify_draft_request(prompt: str) -> str:
    lowered = prompt.lower()
    if "founder update" in lowered:
        return "founder_update"
    if "weekly" in lowered and "update" in lowered:
        return "weekly_update"
    if "summarize" in lowered or "summary" in lowered:
        return "summarize"
    if "recommendation" in lowered or "recommend " in lowered or lowered.startswith("recommend"):
        return "product_recommendation"
    return "generic"


def _is_generic_founder_update_request(prompt: str) -> bool:
    lowered = prompt.lower().strip()
    generic_markers = [
        "write a founder update",
        "write a founder update in my style",
        "draft: write a founder update",
        "draft: write a founder update in my style",
        "draft a founder update in my style",
        "founder update in my style",
        "write founder update in my style",
    ]
    return any(marker in lowered for marker in generic_markers)


def _draft_type_instructions(draft_type: str) -> str:
    if draft_type == "founder_update":
        return (
            "Draft type: founder update.\n"
            "Write like a concise operator update.\n"
            "Prefer exactly 3 short bullets when the user's preferences mention structure or bullets.\n"
            "Use concrete status language, not roadmap boilerplate.\n"
            "Avoid phrases like 'significant progress', 'core values', 'move forward', 'key milestones', and 'next quarter' unless directly required.\n"
            "Avoid inspirational or visionary filler.\n\n"
        )
    if draft_type == "weekly_update":
        return (
            "Draft type: weekly execution update.\n"
            "Prefer bullets for progress, risks, and next steps when appropriate.\n"
            "Keep it concrete and execution-focused.\n\n"
        )
    if draft_type == "product_recommendation":
        return (
            "Draft type: product recommendation.\n"
            "Use exactly 3 bullets labeled Recommendation, Tradeoff, and Risk.\n"
            "State the recommendation first.\n"
            "Then include tradeoffs and explicit risks.\n"
            "Do not just restate the user's preferences.\n\n"
        )
    if draft_type == "summarize":
        return (
            "Draft type: summarize.\n"
            "Summarize the content directly.\n"
            "Do not ask extra questions unless the request is missing the content to summarize.\n"
            "Keep the summary clean, concise, and in English.\n\n"
        )
    return ""


def _enforce_draft_shape(text: str, *, draft_type: str, user_id: str | None) -> str:
    if draft_type == "product_recommendation":
        return _shape_product_recommendation(text, user_id=user_id)
    if draft_type == "founder_update" and _user_prefers_bullets(user_id):
        return _shape_founder_update(text, user_id=user_id)
    if draft_type == "generic":
        return _clean_reply_response(text, user_id=user_id, prompt="")
    return text


def _shape_product_recommendation(text: str, *, user_id: str | None) -> str:
    recommendation, tradeoff, risk = _memory_grounded_recommendation(user_id)
    return "\n".join(
        [
            f"- Recommendation: {recommendation}",
            f"- Tradeoff: {tradeoff}",
            f"- Risk: {risk}",
        ]
    )


def _shape_founder_update(text: str, *, user_id: str | None) -> str:
    stripped = text.strip()
    if (stripped.startswith("- ") or stripped.startswith("• ")) and not _looks_like_generic_founder_update(stripped):
        return stripped

    if _looks_like_generic_founder_update(stripped):
        progress, risk, next_step = _memory_grounded_founder_update(user_id)
        return _render_founder_update(progress, risk, next_step, user_id=user_id)

    sentences = _sentence_chunks(stripped)
    if not sentences:
        progress, risk, next_step = _memory_grounded_founder_update(user_id)
        return _render_founder_update(progress, risk, next_step, user_id=user_id)

    labels = ["Progress", "Risk", "Next"]
    bullets: list[str] = []
    for idx, sentence in enumerate(sentences[:3]):
        bullets.append(f"- {labels[idx]}: {sentence}")
    return "\n".join(bullets)


def _render_founder_update(progress: str, risk: str, next_step: str, *, user_id: str | None) -> str:
    controls = _memory_style_controls(user_id)
    if controls["bullets"]:
        return "\n".join(
            [
                f"- Progress: {progress}",
                f"- Risk: {risk}",
                f"- Next: {next_step}",
            ]
        )
    if controls["concise"]:
        return (
            f"Progress: {progress} "
            f"Risk: {risk} "
            f"Next: {next_step}"
        )
    return "\n".join(
        [
            f"- Progress: {progress}",
            f"- Risk: {risk}",
            f"- Next: {next_step}",
        ]
    )


def _sentence_chunks(text: str) -> list[str]:
    prepared = text.replace("\n", " ").strip()
    if not prepared:
        return []
    chunks = [part.strip(' "')
              for part in prepared.split(".")
              if part.strip(' "')]
    normalized: list[str] = []
    for chunk in chunks:
        if chunk.endswith(("?", "!")):
            normalized.append(chunk)
        else:
            normalized.append(f"{chunk}.")
    return normalized


def _user_prefers_bullets(user_id: str | None) -> bool:
    mem = get_memory_dir(user_id)
    preferences = _parse_bullets((mem / "PREFERENCES.md").read_text(encoding="utf-8"))
    return any("bullet" in item.lower() or "structured" in item.lower() for item in preferences)


def _memory_style_controls(user_id: str | None) -> dict[str, bool]:
    mem = get_memory_dir(user_id)
    derived = read_active_derived_facts(mem)
    preferences = _parse_bullets((mem / "PREFERENCES.md").read_text(encoding="utf-8"))
    corrections = _parse_bullets((mem / "CORRECTIONS.md").read_text(encoding="utf-8"))
    lowered_preferences = [item.lower() for item in preferences]
    lowered_corrections = [item.lower() for item in corrections]

    concise = "concise" in derived.get("style.detail_level", []) or any("concise" in item or "short" in item for item in lowered_preferences)
    no_emojis = "deny" in derived.get("style.emoji_policy", []) or any("do not use emojis" in item for item in lowered_corrections)
    bullets = "bullets" in derived.get("style.output_format", []) or any("bullet" in item or "structured" in item for item in lowered_preferences)
    return {
        "concise": concise,
        "no_emojis": no_emojis,
        "bullets": bullets,
    }


def _memory_entries(user_id: str | None, name: str) -> list[str]:
    mem = get_memory_dir(user_id)
    return _parse_bullets((mem / name).read_text(encoding="utf-8"))


def _memory_grounded_recommendation(user_id: str | None) -> tuple[str, str, str]:
    preferences = _memory_entries(user_id, "PREFERENCES.md")
    corrections = _memory_entries(user_id, "CORRECTIONS.md")
    lowered = [item.lower() for item in preferences]
    wants_tradeoffs = any("tradeoff" in item for item in lowered)
    wants_risks = any("explicit risks" in item for item in lowered)
    direct = any("direct answers" in item for item in lowered)
    recommendation = "Recommend the option that makes the tradeoffs explicit and can be validated quickly before a broader commitment."
    if direct and wants_tradeoffs:
        recommendation = "Recommend the option that gives the clearest tradeoffs, the fastest path to a decision, and an explicit risk review before rollout."
    tradeoff = "You gain clarity and speed, but you may give up flexibility or depth compared with a more complex option."
    if not wants_tradeoffs:
        tradeoff = "The tradeoff is faster execution now versus optionality later."
    risk = "Without product-specific context, this should be treated as a decision framework rather than a final choice."
    if wants_risks:
        risk = "The main risk is overcommitting before you have enough product-specific evidence, so validate with a small test first."
    if any("do not hide uncertainty" in item.lower() for item in corrections):
        risk = "I do not have enough product-specific context to make a final call, so treat this as a working recommendation and validate it with a small test first."
    return recommendation, tradeoff, risk


def _looks_like_generic_founder_update(text: str) -> bool:
    lowered = text.lower()
    generic_markers = [
        "significant progress",
        "q2 launch",
        "q3",
        "core values",
        "move forward",
        "key milestones",
        "strong potential for growth",
        "positive impact",
        "[industry/field]",
        "series a",
        "website traffic",
        "crm system",
        "customer engagement metrics",
        "secured $",
        "funding round",
        "marketing campaign",
    ]
    return any(marker in lowered for marker in generic_markers)


def _memory_grounded_founder_update(user_id: str | None) -> tuple[str, str, str]:
    beliefs = _memory_entries(user_id, "BELIEFS.md")
    preferences = _memory_entries(user_id, "PREFERENCES.md")
    corrections = _memory_entries(user_id, "CORRECTIONS.md")
    lowered = [item.lower() for item in beliefs + preferences]
    lowered_corrections = [item.lower() for item in corrections]
    wants_speed_vs_stability = any("execution speed" in item and "stability" in item for item in lowered)
    wants_tradeoffs = any("tradeoff" in item for item in lowered)
    progress = "We tightened execution around the highest-priority work and kept delivery moving without expanding scope."
    risk = "The main risk is pushing speed too hard and creating stability debt that slows us later."
    next_step = "Next, we keep the release plan tight, validate the highest-risk assumptions, and only ship what is ready."
    if wants_speed_vs_stability:
        progress = "We kept execution moving on the highest-priority work while holding the line on stability."
        risk = "The main risk is forcing speed where the system still needs more validation."
        next_step = "Next, we will close the highest-risk gaps first, then ship the narrowest stable increment."
    if wants_tradeoffs:
        risk = "The main tradeoff is speed now versus stability later; we are choosing the narrower path that preserves reliability."
    if any("generic motivational language" in item for item in lowered_corrections):
        progress = progress.replace("highest-priority", "priority")
    return progress, risk, next_step


def _generate_direct_draft_template(user_id: str | None, prompt: str, *, draft_type: str) -> str | None:
    if draft_type != "generic":
        return None
    if _is_meeting_reschedule_request(prompt):
        return _build_meeting_reschedule_reply(user_id, prompt)
    return None


def _is_meeting_reschedule_request(prompt: str) -> bool:
    lowered = prompt.lower()
    return "meeting" in lowered and ("move" in lowered or "moving" in lowered or "reschedule" in lowered)


def _build_meeting_reschedule_reply(user_id: str | None, prompt: str) -> str:
    controls = _memory_style_controls(user_id)
    lowered = prompt.lower()
    target = "tomorrow's meeting" if "tomorrow" in lowered else "the meeting"
    if controls["concise"]:
        return f"Can we move {target}? Share a time that works for you."
    return f"Can we move {target} to another time? What works for you?"


def _is_underspecified_summarize_request(prompt: str) -> bool:
    lowered = " ".join(prompt.lower().replace(":", " ").split())
    weak_patterns = (
        "summarize",
        "summary",
        "summarize this",
        "summarize this update",
        "summarize this update in my style",
        "summary of this update",
    )
    return any(lowered == pattern or lowered.startswith(f"{pattern} ") for pattern in weak_patterns)


def _generic_summarize_clarification() -> str:
    return (
        "Please send the text or update you want summarized.\n"
        "Example: Summarize: <paste the update here>"
    )


def _memory_grounded_summarize_clarification(user_id: str | None) -> str:
    preferences = _memory_entries(user_id, "PREFERENCES.md")
    corrections = _memory_entries(user_id, "CORRECTIONS.md")
    style_hint = ""
    lowered_prefs = [item.lower() for item in preferences]
    if any("bullet" in item or "structured" in item for item in lowered_prefs):
        style_hint = " I’ll keep it structured and easy to scan."
    elif any("concise" in item for item in lowered_prefs):
        style_hint = " I’ll keep it concise."
    correction_hint = ""
    if any("do not use emojis" in item.lower() for item in corrections):
        correction_hint = " I’ll avoid emojis."
    return (
        "Please send the text or update you want summarized."
        f"{style_hint}{correction_hint}\n"
        "Example: Summarize: <paste the update here>"
    )


def _clean_reply_response(text: str, *, user_id: str | None, prompt: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    if prompt and _is_meeting_reschedule_request(prompt):
        return _build_meeting_reschedule_reply(user_id, prompt)

    cleaned = re.sub(r"^(here is|here's|below is)\s+(a\s+)?(reply|response|draft):?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^i received your message(?:\s+and)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^thanks for reaching out[,.]?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r'(?i)\s*please suggest a preferred slot, and i\'ll adjust my calendar accordingly\.?$',
        " What works for you?",
        cleaned,
    ).strip()

    controls = _memory_style_controls(user_id)
    if controls["concise"]:
        cleaned = _truncate_to_short_reply(cleaned, max_words=24, max_sentences=2)
    return cleaned


def _truncate_to_short_reply(text: str, *, max_words: int, max_sentences: int) -> str:
    sentences = _sentence_chunks(text)
    if sentences:
        text = " ".join(sentences[:max_sentences]).strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,;:")
        if not text.endswith((".", "?", "!")):
            text += "."
    return text
