from __future__ import annotations

import re
from collections import Counter

from eightmem.core.models import ChatMessage, Signals


IDENTITY_PATTERNS = [
    re.compile(r"\b(?:i am|i'm)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bmy name is\s+([^.,;]+?)(?=\s+and\b|[.,;]|$)", re.IGNORECASE),
    re.compile(r"\bi work as\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi(?:'m| am) building\s+(.+)", re.IGNORECASE),
]

PREFERENCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bkeep answers?\s+(.+)", re.IGNORECASE), "keep"),
    (re.compile(r"\b(?:please\s+|kindly\s+)?avoid\s+(.+)", re.IGNORECASE), "avoid"),
    (re.compile(r"\b(?:please\s+|kindly\s+)?(?:don't|do not)\s+(.+)", re.IGNORECASE), "dont"),
    (re.compile(r"\bi prefer\s+(.+)", re.IGNORECASE), "prefer"),
]

CORRECTION_HINTS = ("don't", "don’t", "do not", "instead", "correction", "that's wrong", "that’s wrong", "not ")
DECISION_PATTERN = re.compile(r"\b(?:we decided|let's|i decided)\s+(.+)", re.IGNORECASE)
EVOLUTION_HINTS = ("started", "changed", "moved to", "now", "recently", "from now on")
CORRECTION_PREFIX = re.compile(r"^\s*corrections?\s*[:\-]\s*(.+)$", re.IGNORECASE)
DECISION_PREFIX = re.compile(r"^\s*decisions?\s*[:\-]\s*(.+)$", re.IGNORECASE)
PREFERENCE_PREFIX = re.compile(r"^\s*preferences?(?:\s+update)?\s*[:\-]\s*(.+)$", re.IGNORECASE)


def extract_signals(messages: list[ChatMessage]) -> Signals:
    signals = Signals()
    topic_counter: Counter[str] = Counter()

    for msg in messages:
        text = _clean(msg.text)
        if not text:
            continue

        role = msg.role.lower()
        if role in {"correction", "corrections"}:
            for correction in _extract_corrections(text):
                signals.corrections.add(correction)
            if any(token in text.lower() for token in EVOLUTION_HINTS):
                signals.evolution.add(_truncate_sentence(text))
            continue
        if role in {"decision", "decisions"}:
            signals.decisions.add(_truncate_sentence(text))
            continue
        if role.startswith("preference"):
            rendered = _render_directive_preference(text)
            if rendered:
                signals.preferences.add(rendered)
            continue

        prefixed = _extract_prefixed_signal(text)
        if prefixed is not None:
            kind, body = prefixed
            if kind == "correction":
                for correction in _extract_corrections(body, force=True):
                    signals.corrections.add(correction)
                if any(token in body.lower() for token in EVOLUTION_HINTS):
                    signals.evolution.add(_truncate_sentence(body))
            elif kind == "decision":
                signals.decisions.add(_truncate_sentence(body))
            elif kind == "preference":
                rendered = _render_directive_preference(body)
                if rendered:
                    signals.preferences.add(rendered)
            continue

        for pattern in IDENTITY_PATTERNS:
            match = pattern.search(text)
            if match:
                signals.identity.add(_normalize(match.group(1)))

        for pref in _extract_preferences(text):
            signals.preferences.add(pref)

        if any(hint in text.lower() for hint in CORRECTION_HINTS) and msg.role in {
            "user",
            "human",
            "unknown",
        }:
            for correction in _extract_corrections(text):
                signals.corrections.add(correction)

        dec = DECISION_PATTERN.search(text)
        if dec:
            signals.decisions.add(_truncate_sentence(_normalize(dec.group(1))))

        if any(token in text.lower() for token in EVOLUTION_HINTS):
            signals.evolution.add(_truncate_sentence(text))

        for token in _keywords(text):
            topic_counter[token] += 1

    for topic, count in topic_counter.most_common(8):
        if count >= 2:
            signals.beliefs.add(f"Likely recurring theme: {topic} (seen {count} times).")

    return signals


def _extract_prefixed_signal(text: str) -> tuple[str, str] | None:
    for pattern, kind in (
        (CORRECTION_PREFIX, "correction"),
        (DECISION_PREFIX, "decision"),
        (PREFERENCE_PREFIX, "preference"),
    ):
        match = pattern.match(text)
        if not match:
            continue
        body = _normalize(match.group(1))
        if body:
            return kind, body
    return None


def _clean(text: str) -> str:
    return " ".join(text.strip().split())


def _normalize(text: str) -> str:
    out = text.strip(" .,!?:;\"'")
    return out[:180]


def _truncate_sentence(text: str, max_len: int = 180) -> str:
    text = _normalize(text)
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."


def _keywords(text: str) -> list[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "you",
        "your",
        "about",
        "have",
        "has",
        "are",
        "was",
        "were",
        "please",
        "keep",
        "answers",
        "use",
        "using",
        "like",
        "just",
        "want",
        "need",
        "prefer",
        "prefers",
        "avoid",
        "numbered",
        "steps",
    }
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    return [token for token in tokens if token not in stop]


def _extract_preferences(text: str) -> set[str]:
    preferences: set[str] = set()
    for sentence in _split_sentences(text):
        for pattern, kind in PREFERENCE_PATTERNS:
            match = pattern.search(sentence)
            if not match:
                continue
            clause = _normalize_clause(match.group(1))
            if not clause:
                continue
            rendered = _render_preference(kind, clause)
            if rendered:
                preferences.add(rendered)
    return preferences


def _split_sentences(text: str) -> list[str]:
    chunks = re.split(r"[.;!\n]+", text)
    return [_clean(chunk) for chunk in chunks if _clean(chunk)]


def _normalize_clause(clause: str) -> str:
    cleaned = _normalize(clause)
    cleaned = re.sub(r"^(that|to)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _render_preference(kind: str, clause: str) -> str | None:
    if kind == "keep":
        return f"Keep answers {clause}."
    if kind == "avoid":
        return f"Avoid {clause}."
    if kind == "dont":
        if clause.lower().startswith("use "):
            return f"Avoid {clause[4:].strip()}."
        return f"Do not {clause}."
    if kind == "prefer":
        return f"Prefers {clause}."
    return None


def _render_directive_preference(body: str) -> str | None:
    clause = _normalize_clause(body)
    if not clause:
        return None
    lower = clause.lower()
    if lower.startswith("avoid "):
        return f"Avoid {clause[6:].strip()}."
    if lower.startswith("don't "):
        return f"Do not {clause[6:].strip()}."
    if lower.startswith("do not "):
        return f"Do not {clause[7:].strip()}."
    if lower.startswith("use "):
        return f"Prefers {clause[4:].strip()}."
    return f"Prefers {clause}."


def _extract_corrections(text: str, *, force: bool = False) -> set[str]:
    corrections: set[str] = set()
    for sentence in _split_sentences(text):
        lowered = sentence.lower().replace("’", "'")
        if not force and not any(hint in lowered for hint in CORRECTION_HINTS):
            continue
        normalized = _normalize_correction_sentence(sentence)
        if normalized:
            corrections.add(normalized)
    return corrections


def _normalize_correction_sentence(text: str) -> str:
    clause = _normalize_clause(text).replace("’", "'")
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
    if lowered.startswith("don't "):
        return f"do not {clause[6:].strip()}"
    if lowered.startswith("do not "):
        return f"do not {clause[7:].strip()}"
    return _truncate_sentence(clause)
