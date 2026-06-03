from __future__ import annotations

from pathlib import Path

from eightmem.analyzers.extractor import extract_signals
from eightmem.core.memory_store import update_memory_files
from eightmem.importers.parser import load_messages


def analyze_chat_export(path: Path, memory_dir: Path) -> dict[str, int]:
    messages = load_messages(path)
    signals = extract_signals(messages)
    return update_memory_files(memory_dir, signals.as_file_map())
