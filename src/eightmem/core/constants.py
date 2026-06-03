from __future__ import annotations

MEMORY_FILES: dict[str, str] = {
    "IDENTITY.md": "Who the user is and what they are focused on.",
    "BELIEFS.md": "Inferred patterns and likely traits based on recurring evidence.",
    "PREFERENCES.md": "Stable response and formatting preferences.",
    "CORRECTIONS.md": "Persistent corrections made by the user.",
    "EVOLUTION.md": "Timeline of meaningful changes over time.",
    "DECISIONS.md": "Important decisions, rationale, and tradeoffs.",
}

MEMORY_ORDER = [
    "IDENTITY.md",
    "BELIEFS.md",
    "PREFERENCES.md",
    "CORRECTIONS.md",
    "EVOLUTION.md",
    "DECISIONS.md",
]
