from __future__ import annotations

from pathlib import Path

from eightmem.core.constants import MEMORY_FILES, MEMORY_ORDER
from eightmem.core.sqlite_facts import sync_memory_file_to_sqlite


HEADER_PREFIX = "# "


def read_memory_files(memory_dir: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for name in MEMORY_ORDER:
        path = memory_dir / name
        data[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return data


def update_memory_files(memory_dir: Path, updates: dict[str, set[str]]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for name in MEMORY_ORDER:
        path = memory_dir / name
        existing = _parse_bullets(path.read_text(encoding="utf-8") if path.exists() else "")
        merged = set(existing)
        merged.update(item for item in updates.get(name, set()) if item)
        ordered = sorted(merged, key=str.lower)
        stats[name] = len(ordered) - len(existing)
        content = _render(name, ordered)
        path.write_text(content, encoding="utf-8")
        sync_memory_file_to_sqlite(memory_dir, name, ordered)
    return stats


def _parse_bullets(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            if value and value != "(empty)":
                items.append(value)
    return items


def _render(name: str, items: list[str]) -> str:
    title = name.replace(".md", "")
    description = MEMORY_FILES.get(name, "")
    lines = [f"{HEADER_PREFIX}{title}", "", description, "", "## Entries", ""]
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- (empty)")
    lines.append("")
    return "\n".join(lines)
