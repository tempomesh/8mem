from __future__ import annotations

from pathlib import Path

from eightmem.core.constants import MEMORY_FILES
from eightmem.core.constants import MEMORY_ORDER
from eightmem.core.memory_store import _parse_bullets, read_memory_files
from eightmem.core.sqlite_facts import read_active_sections_from_sqlite, sqlite_has_active_facts


def compile_context(memory_dir: Path) -> str:
    lines = ["# 8mem Context Export", "", "Use this as persistent memory context:", ""]
    if sqlite_has_active_facts(memory_dir):
        sections = read_active_sections_from_sqlite(memory_dir)
    else:
        files = read_memory_files(memory_dir)
        sections = {name: _parse_bullets(files.get(name, "")) for name in MEMORY_ORDER}
    for name in MEMORY_ORDER:
        title = name.replace(".md", "")
        entries = sections.get(name, [])
        lines.append(f"## {title}")
        description = MEMORY_FILES.get(name, "")
        if description:
            lines.append(description)
            lines.append("")
        lines.append("## Entries")
        if entries:
            lines.extend(f"- {item}" for item in entries)
        else:
            lines.append("- (empty)")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
