from __future__ import annotations

from pathlib import Path

from eightmem.core.constants import MEMORY_ORDER
from eightmem.core.memory_store import read_memory_files


def build_mirror_text(memory_dir: Path) -> str:
    files = read_memory_files(memory_dir)
    sections: list[str] = ["AI Believes About You", "=" * 24, ""]

    for name in MEMORY_ORDER:
        section_name = name.replace(".md", "").title()
        items = _extract_bullets(files.get(name, ""))
        sections.append(f"{section_name}:")
        if items:
            sections.extend([f"  - {item}" for item in items[:12]])
        else:
            sections.append("  - (no entries yet)")
        sections.append("")

    tensions = _detect_tensions(files.get("PREFERENCES.md", ""), files.get("CORRECTIONS.md", ""))
    sections.append("Possible Contradictions:")
    if tensions:
        sections.extend([f"  - {item}" for item in tensions])
    else:
        sections.append("  - None detected.")
    sections.append("")

    return "\n".join(sections)


def mirror_sections(memory_dir: Path) -> dict[str, list[str]]:
    files = read_memory_files(memory_dir)
    return {name: _extract_bullets(text) for name, text in files.items()}


def _extract_bullets(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:] != "(empty)":
            items.append(stripped[2:])
    return items


def _detect_tensions(preferences_text: str, corrections_text: str) -> list[str]:
    tensions: list[str] = []
    pref_items = _extract_bullets(preferences_text)
    corr_items = _extract_bullets(corrections_text)
    prefs = " ".join(pref_items).lower()
    corrs = " ".join(corr_items).lower()

    has_concise_or_short = _contains_any(pref_items, ["concise", "short"])
    has_detailed = _contains_any(
        pref_items,
        ["detailed", "detail", "detailed explanations", "long detailed explanations", "long explanations", "full explanations"],
    )
    has_structured_or_bullets = _contains_any(pref_items, ["bullet", "bullets", "structured"])
    has_long_form = _contains_any(
        pref_items,
        [
            "long paragraphs",
            "long narrative paragraphs",
            "narrative paragraphs",
            "essay",
            "essays",
            "detailed explanations",
            "long detailed explanations",
            "long explanations",
            "full explanations",
        ],
    )

    if has_structured_or_bullets and has_long_form:
        tensions.append("Preference guidance conflicts: structured bullets versus long detailed explanations.")
    elif has_concise_or_short and has_detailed:
        tensions.append("Preference guidance conflicts: concise answers versus detailed explanations.")

    if "short" in prefs and ("detailed" in corrs or "more detail" in corrs):
        tensions.append("Prefers short answers but later requested more detail.")
    if ("concise" in prefs or "short" in prefs) and (
        ("long paragraphs" in corrs and ("avoid" not in corrs and "do not" not in corrs and "don't" not in corrs))
        or "detailed" in corrs
    ):
        tensions.append("Concise-answer preference may conflict with later long-form guidance.")
    if "avoid jargon" in prefs and "use technical terms" in corrs:
        tensions.append("Avoid-jargon preference may conflict with technical-term correction.")
    if "emoji" in prefs and "emoji" in corrs and ("don't" in corrs or "do not" in corrs):
        tensions.append("Emoji guidance appears inconsistent across notes.")

    return list(dict.fromkeys(tensions))


def _contains_any(items: list[str], needles: list[str]) -> bool:
    lowered = " ".join(item.lower() for item in items)
    return any(needle in lowered for needle in needles)
