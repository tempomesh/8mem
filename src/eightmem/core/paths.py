from __future__ import annotations

import os
from pathlib import Path


ENV_HOME = "EIGHTMEM_HOME"
USERS_DIR = "users"
MEMORY_DIRNAME = "memory"
CACHE_DIRNAME = "cache"


def runtime_home() -> Path:
    override = os.getenv(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".8mem"


def memory_dir() -> Path:
    return runtime_home() / MEMORY_DIRNAME


def user_home(user_id: str) -> Path:
    return runtime_home() / USERS_DIR / user_id


def user_memory_dir(user_id: str) -> Path:
    return user_home(user_id) / MEMORY_DIRNAME


def ensure_runtime_dirs() -> tuple[Path, Path]:
    home = runtime_home()
    mem = memory_dir()
    home.mkdir(parents=True, exist_ok=True)
    mem.mkdir(parents=True, exist_ok=True)
    return home, mem


def ensure_user_runtime_dirs(user_id: str) -> tuple[Path, Path]:
    home = user_home(user_id)
    mem = user_memory_dir(user_id)
    home.mkdir(parents=True, exist_ok=True)
    mem.mkdir(parents=True, exist_ok=True)
    return home, mem


def cache_dir() -> Path:
    return runtime_home() / CACHE_DIRNAME


def ensure_cache_dir() -> Path:
    path = cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
