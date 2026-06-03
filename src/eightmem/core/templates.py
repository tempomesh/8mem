from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from eightmem.core.constants import MEMORY_ORDER


def template_dir() -> Path:
    return Path(str(files("eightmem.resources").joinpath("memory_templates")))


def copy_default_templates(memory_dir: Path) -> list[Path]:
    source_dir = template_dir()
    created: list[Path] = []
    for name in MEMORY_ORDER:
        src = source_dir / name
        dst = memory_dir / name
        if not dst.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(dst)
    return created
