from __future__ import annotations

import math
import re
from typing import Any

from eightmem.services.memory_service import (
    build_active_memory_source_map,
    build_engram_context,
)


DEFAULT_CONTEXT_TOKEN_BUDGET = 1200
DEFAULT_CONTEXT_ITEM_LIMIT = 20
MIN_CONTEXT_TOKEN_BUDGET = 32
MAX_CONTEXT_TOKEN_BUDGET = 32_000
MAX_CONTEXT_ITEM_LIMIT = 100

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_CATEGORY_PRIORITY = {
    "correction": 0.70,
    "preference": 0.55,
    "identity": 0.45,
    "decision": 0.40,
    "belief": 0.35,
    "evolution": 0.15,
}


def compile_governed_context(
    *,
    user_id: str | None,
    query: str,
    max_tokens: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    max_items: int = DEFAULT_CONTEXT_ITEM_LIMIT,
    issuer: str | None = None,
) -> dict[str, Any]:
    """Compile active, user-scoped memory into a deterministic token budget."""
    query = query.strip()
    if not query:
        raise ValueError("query must be a non-empty string")
    if not MIN_CONTEXT_TOKEN_BUDGET <= max_tokens <= MAX_CONTEXT_TOKEN_BUDGET:
        raise ValueError(
            f"max_tokens must be between {MIN_CONTEXT_TOKEN_BUDGET} and {MAX_CONTEXT_TOKEN_BUDGET}"
        )
    if not 1 <= max_items <= MAX_CONTEXT_ITEM_LIMIT:
        raise ValueError(f"max_items must be between 1 and {MAX_CONTEXT_ITEM_LIMIT}")

    engram = build_engram_context(user_id=user_id, issuer=issuer)
    memory_sources = build_active_memory_source_map(user_id=user_id)
    candidates = _active_candidates(
        engram.get("beliefs", []),
        query=query,
        memory_sources=memory_sources,
        superseded_values=_superseded_values(engram.get("corrections", [])),
    )
    source_tokens = sum(_estimate_tokens(_render_context_line(item)) for item in candidates)

    selected: list[dict[str, Any]] = []
    output_tokens = 0
    for candidate in candidates:
        if len(selected) >= max_items:
            break
        line_tokens = _estimate_tokens(_render_context_line(candidate))
        if output_tokens + line_tokens > max_tokens:
            continue
        selected.append(candidate)
        output_tokens += line_tokens

    context_text = "\n".join(_render_context_line(item) for item in selected)
    omitted_count = len(candidates) - len(selected)
    tokens_avoided = max(source_tokens - output_tokens, 0)
    compression_ratio = round(tokens_avoided / source_tokens, 4) if source_tokens else 0.0

    return {
        "ok": True,
        "context_version": "v1",
        "subject": engram["subject"],
        "scope": {
            "type": "user",
            "user_id": engram["subject"],
        },
        "query": query,
        "identity": engram["identity"],
        "context_text": context_text,
        "items": selected,
        "compression": {
            "strategy": "deterministic_relevance_v1",
            "max_tokens": max_tokens,
            "source_item_count": len(candidates),
            "selected_item_count": len(selected),
            "omitted_item_count": omitted_count,
            "source_tokens_estimated": source_tokens,
            "output_tokens_estimated": output_tokens,
            "tokens_avoided_estimated": tokens_avoided,
            "compression_ratio": compression_ratio,
            "truncated": omitted_count > 0,
        },
        "governance": {
            "active_only": True,
            "corrections_applied": True,
            "forget_enforced": True,
            "user_scope_enforced": True,
            "mutation_allowed": False,
        },
        "issued_at": engram["issued_at"],
        "expires_at": engram["expires_at"],
        "issuer": engram["issuer"],
    }


def _active_candidates(
    raw_beliefs: object,
    *,
    query: str,
    memory_sources: dict[str, str],
    superseded_values: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_beliefs, list):
        return []

    query_tokens = _tokens(query)
    query_normalized = " ".join(query.lower().split())
    candidates_by_value: dict[str, dict[str, Any]] = {}
    for raw in raw_beliefs:
        if not isinstance(raw, dict) or raw.get("status") != "active":
            continue
        value = str(raw.get("value") or "").strip()
        if not value:
            continue
        normalized_value = " ".join(value.lower().split())
        source_class = str(raw.get("source") or "unknown")
        if normalized_value in superseded_values or source_class == "evolution":
            continue
        category = str(raw.get("category") or "memory")
        if category == "memory" and source_class in _CATEGORY_PRIORITY:
            category = source_class
        score = _relevance_score(
            query_normalized=query_normalized,
            query_tokens=query_tokens,
            value=value,
            category=category,
        )
        candidate = {
            "id": str(raw.get("id") or ""),
            "category": category,
            "value": value,
            "source": memory_sources.get(_memory_key(value), source_class),
            "source_class": source_class,
            "confidence": _normalized_confidence(raw.get("confidence")),
            "status": "active",
            "relevance_score": score,
        }
        existing = candidates_by_value.get(normalized_value)
        if existing is None or _candidate_sort_key(candidate) < _candidate_sort_key(existing):
            candidates_by_value[normalized_value] = candidate

    return sorted(candidates_by_value.values(), key=_candidate_sort_key)


def _superseded_values(raw_corrections: object) -> set[str]:
    if not isinstance(raw_corrections, list):
        return set()
    values: set[str] = set()
    for correction in raw_corrections:
        if not isinstance(correction, dict):
            continue
        old_value = str(correction.get("old_value") or "").strip()
        if old_value:
            values.add(" ".join(old_value.lower().split()))
    return values


def _relevance_score(
    *,
    query_normalized: str,
    query_tokens: set[str],
    value: str,
    category: str,
) -> float:
    value_normalized = " ".join(value.lower().split())
    value_tokens = _tokens(value)
    overlap = len(query_tokens & value_tokens)
    overlap_ratio = overlap / len(query_tokens) if query_tokens else 0.0
    phrase_bonus = 0.20 if query_normalized and query_normalized in value_normalized else 0.0
    category_score = _CATEGORY_PRIORITY.get(category, 0.25)
    return round(min(category_score + (0.55 * overlap_ratio) + phrase_bonus, 1.0), 4)


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, float, str]:
    return (
        -float(candidate["relevance_score"]),
        -float(candidate["confidence"]),
        str(candidate["id"]),
    )


def _normalized_confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return round(max(0.0, min(float(value), 1.0)), 4)
    return 0.0


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.lower()))


def _memory_key(value: str) -> str:
    return " ".join(value.lower().strip().rstrip(".").split())


def _render_context_line(item: dict[str, Any]) -> str:
    return f"- [{item['category']}] {item['value']}"


def _estimate_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))
