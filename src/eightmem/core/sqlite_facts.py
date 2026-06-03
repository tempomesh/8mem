from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from eightmem.core.constants import MEMORY_ORDER

try:
    import sqlite_vec
except Exception:  # pragma: no cover - optional dependency
    sqlite_vec = None


DB_NAME = ".memory_facts.sqlite3"

_PREDICATE_BY_FILE = {
    "IDENTITY.md": "identity.statement",
    "BELIEFS.md": "belief.statement",
    "PREFERENCES.md": "preference.default",
    "CORRECTIONS.md": "correction.hard",
    "EVOLUTION.md": "evolution.note",
    "DECISIONS.md": "decision.record",
}

DERIVED_FILE_NAME = "__derived__"
_CITY_RE = r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*"
SEMANTIC_DIM = 64


def db_path_for_memory_dir(memory_dir: Path) -> Path:
    return memory_dir.parent / DB_NAME


def ensure_sqlite_memory_store(memory_dir: Path) -> Path:
    path = db_path_for_memory_dir(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                scope TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                file_name TEXT,
                detail TEXT NOT NULL,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_file_name ON facts(file_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_scope_active ON facts(scope, valid_to)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_active_unique "
            "ON facts(file_name, normalized_text, scope) WHERE valid_to IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_events_ts ON memory_events(ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_key TEXT NOT NULL,
                text_value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_semantic_items_scope ON semantic_items(scope)"
        )
        conn.commit()
    _ensure_vec_virtual_table(path)
    return path


def sync_memory_file_to_sqlite(memory_dir: Path, file_name: str, items: list[str]) -> None:
    path = ensure_sqlite_memory_store(memory_dir)
    now = _utcnow()
    predicate = _PREDICATE_BY_FILE.get(file_name, "memory.statement")
    scope = _scope_for_memory_dir(memory_dir)
    normalized = {_normalize(item): item for item in items if item.strip()}
    with sqlite3.connect(path) as conn:
        existing_rows = conn.execute(
            """
            SELECT id, normalized_text
            FROM facts
            WHERE file_name = ? AND scope = ? AND valid_to IS NULL
            """,
            (file_name, scope),
        ).fetchall()
        existing = {row[1]: row[0] for row in existing_rows}

        for norm, row_id in existing.items():
            if norm not in normalized:
                conn.execute(
                    "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ?",
                    (now, now, row_id),
                )

        for norm, original in normalized.items():
            if norm in existing:
                conn.execute(
                    "UPDATE facts SET object_text = ?, updated_at = ? WHERE id = ?",
                    (original, now, existing[norm]),
                )
                continue
            conn.execute(
                """
                INSERT INTO facts (
                    file_name,
                    subject,
                    predicate,
                    object_text,
                    normalized_text,
                    scope,
                    valid_from,
                    valid_to,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    file_name,
                    "user",
                    predicate,
                    original,
                    norm,
                    scope,
                    now,
                    now,
                    now,
                ),
            )
        conn.commit()


def sync_memory_dir_to_sqlite(memory_dir: Path, files: dict[str, list[str]]) -> None:
    ensure_sqlite_memory_store(memory_dir)
    for file_name in MEMORY_ORDER:
        sync_memory_file_to_sqlite(memory_dir, file_name, files.get(file_name, []))
    sync_derived_facts(memory_dir, files)
    rebuild_semantic_index(memory_dir)


def sync_derived_facts(memory_dir: Path, files: dict[str, list[str]]) -> None:
    path = ensure_sqlite_memory_store(memory_dir)
    now = _utcnow()
    scope = _scope_for_memory_dir(memory_dir)
    derived_facts = _derive_facts(files)
    normalized = {(predicate, _normalize(object_text)): object_text for predicate, object_text in derived_facts}
    with sqlite3.connect(path) as conn:
        existing_rows = conn.execute(
            """
            SELECT id, predicate, normalized_text
            FROM facts
            WHERE file_name = ? AND scope = ? AND valid_to IS NULL
            """,
            (DERIVED_FILE_NAME, scope),
        ).fetchall()
        existing = {(row[1], row[2]): row[0] for row in existing_rows}

        for key, row_id in existing.items():
            if key not in normalized:
                conn.execute(
                    "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ?",
                    (now, now, row_id),
                )

        for (predicate, norm), object_text in normalized.items():
            if (predicate, norm) in existing:
                conn.execute(
                    "UPDATE facts SET object_text = ?, updated_at = ? WHERE id = ?",
                    (object_text, now, existing[(predicate, norm)]),
                )
                continue
            conn.execute(
                """
                INSERT INTO facts (
                    file_name,
                    subject,
                    predicate,
                    object_text,
                    normalized_text,
                    scope,
                    valid_from,
                    valid_to,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    DERIVED_FILE_NAME,
                    "user",
                    predicate,
                    object_text,
                    norm,
                    scope,
                    now,
                    now,
                    now,
                ),
            )
        conn.commit()


def append_memory_event_to_sqlite(
    memory_dir: Path,
    *,
    ts: str,
    action: str,
    file_name: str | None,
    detail: str,
    payload: dict[str, object],
) -> None:
    path = ensure_sqlite_memory_store(memory_dir)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO memory_events (ts, action, file_name, detail, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, action, file_name, detail, json.dumps(payload, ensure_ascii=True)),
        )
        conn.commit()
    rebuild_semantic_index(memory_dir)


def read_active_sections_from_sqlite(memory_dir: Path) -> dict[str, list[str]]:
    path = ensure_sqlite_memory_store(memory_dir)
    scope = _scope_for_memory_dir(memory_dir)
    sections = {name: [] for name in MEMORY_ORDER}
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT file_name, object_text
            FROM facts
            WHERE scope = ? AND valid_to IS NULL
            ORDER BY file_name, lower(object_text)
            """,
            (scope,),
        ).fetchall()
    for file_name, object_text in rows:
        if file_name in sections:
            sections[file_name].append(object_text)
    return sections


def read_active_derived_facts(memory_dir: Path) -> dict[str, list[str]]:
    path = ensure_sqlite_memory_store(memory_dir)
    scope = _scope_for_memory_dir(memory_dir)
    facts: dict[str, list[str]] = {}
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT predicate, object_text
            FROM facts
            WHERE scope = ? AND file_name = ? AND valid_to IS NULL
            ORDER BY predicate, lower(object_text)
            """,
            (scope, DERIVED_FILE_NAME),
        ).fetchall()
    for predicate, object_text in rows:
        facts.setdefault(predicate, []).append(object_text)
    return facts


def search_semantic_memory(memory_dir: Path, query: str, *, k: int = 3) -> list[dict[str, object]]:
    if not sqlite_vec:
        return []
    path = ensure_sqlite_memory_store(memory_dir)
    scope = _scope_for_memory_dir(memory_dir)
    query = query.strip()
    if not query:
        return []
    with _connect_vec(path) as conn:
        rows = conn.execute(
            """
            SELECT semantic_items.kind, semantic_items.source_key, semantic_items.text_value, semantic_vectors.distance
            FROM semantic_vectors
            JOIN semantic_items ON semantic_items.id = semantic_vectors.id
            WHERE semantic_items.scope = ?
              AND semantic_vectors.embedding MATCH ?
              AND k = ?
            ORDER BY semantic_vectors.distance
            """,
            (scope, sqlite_vec.serialize_float32(_embed_text(query)), k),
        ).fetchall()
    return [
        {
            "kind": kind,
            "source_key": source_key,
            "text": text_value,
            "distance": distance,
        }
        for kind, source_key, text_value, distance in rows
    ]


def sqlite_has_active_facts(memory_dir: Path) -> bool:
    path = ensure_sqlite_memory_store(memory_dir)
    scope = _scope_for_memory_dir(memory_dir)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE scope = ? AND valid_to IS NULL LIMIT 1",
            (scope,),
        ).fetchone()
    return row is not None


def sqlite_vec_available() -> bool:
    return sqlite_vec is not None


def _scope_for_memory_dir(memory_dir: Path) -> str:
    parts = memory_dir.parts
    if "users" in parts:
        idx = parts.index("users")
        if idx + 1 < len(parts):
            return f"user:{parts[idx + 1]}"
    return "default"


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_vec_virtual_table(path: Path) -> None:
    if not sqlite_vec:
        return
    with _connect_vec(path) as conn:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS semantic_vectors
            USING vec0(id INTEGER PRIMARY KEY, embedding float[{SEMANTIC_DIM}])
            """
        )
        conn.commit()


def _connect_vec(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


def rebuild_semantic_index(memory_dir: Path) -> None:
    if not sqlite_vec:
        return
    path = ensure_sqlite_memory_store(memory_dir)
    scope = _scope_for_memory_dir(memory_dir)
    items = _semantic_items_for_scope(path, scope)
    with _connect_vec(path) as conn:
        conn.execute("DELETE FROM semantic_vectors")
        conn.execute("DELETE FROM semantic_items WHERE scope = ?", (scope,))
        for kind, source_key, text_value in items:
            cur = conn.execute(
                "INSERT INTO semantic_items (scope, kind, source_key, text_value) VALUES (?, ?, ?, ?)",
                (scope, kind, source_key, text_value),
            )
            item_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO semantic_vectors(id, embedding) VALUES (?, ?)",
                (item_id, sqlite_vec.serialize_float32(_embed_text(text_value))),
            )
        conn.commit()


def _semantic_items_for_scope(path: Path, scope: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as conn:
        facts = conn.execute(
            """
            SELECT file_name, predicate, object_text
            FROM facts
            WHERE scope = ? AND valid_to IS NULL
            ORDER BY file_name, predicate, lower(object_text)
            """,
            (scope,),
        ).fetchall()
    items: list[tuple[str, str, str]] = []
    for file_name, predicate, object_text in facts:
        if file_name in {DERIVED_FILE_NAME, "EVOLUTION.md"}:
            continue
        items.append(("fact", f"{file_name}:{predicate}", object_text))
    deduped = list(dict.fromkeys(items))
    return deduped


def _embed_text(text: str) -> list[float]:
    vec = [0.0] * SEMANTIC_DIM
    tokens = re.findall(r"[a-zA-Z0-9_]{2,}", text.lower())
    if not tokens:
        return vec
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % SEMANTIC_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = sum(value * value for value in vec) ** 0.5
    if norm == 0:
        return vec
    return [value / norm for value in vec]


def _derive_facts(files: dict[str, list[str]]) -> list[tuple[str, str]]:
    derived: list[tuple[str, str]] = []

    preferences = files.get("PREFERENCES.md", [])
    corrections = files.get("CORRECTIONS.md", [])
    identity = files.get("IDENTITY.md", [])
    evolution = files.get("EVOLUTION.md", [])

    for item in preferences:
        lower = item.lower()
        if "emoji" in lower:
            if "avoid emojis" in lower or "do not use emojis" in lower:
                derived.append(("style.emoji_policy", "deny"))
            elif "use emojis" in lower:
                derived.append(("style.emoji_policy", "allow"))
        if "concise" in lower or "short answers" in lower:
            derived.append(("style.detail_level", "concise"))
        if "detailed explanations" in lower or "long-form" in lower or "long form" in lower:
            derived.append(("style.detail_level", "detailed"))
            derived.append(("style.output_format", "long_form"))
        if "bullet" in lower or "structured" in lower:
            derived.append(("style.output_format", "bullets"))
        if "long narrative paragraphs" in lower or "long-form" in lower or "long form" in lower:
            derived.append(("style.output_format", "long_form"))

    for item in corrections:
        lower = item.lower()
        if "emoji" in lower:
            if "do not use emojis" in lower:
                derived.append(("style.emoji_policy", "deny"))
            elif "use emojis" in lower:
                derived.append(("style.emoji_policy", "allow"))

    for item in identity + evolution:
        if match := re.search(rf"\bmoved to\s+({_CITY_RE})\s+from\s+({_CITY_RE})\b", item):
            derived.append(("person.location.current", match.group(1)))
            derived.append(("person.location.previous", match.group(2)))
            continue
        if match := re.search(rf"\bcurrently live in\s+({_CITY_RE})\b", item, re.IGNORECASE):
            derived.append(("person.location.current", match.group(1)))
            continue
        if match := re.search(rf"\blive in\s+({_CITY_RE})\b", item, re.IGNORECASE):
            derived.append(("person.location.current", match.group(1)))
            continue

    deduped = list(dict.fromkeys(derived))
    current_locations = [value for predicate, value in deduped if predicate == "person.location.current"]
    if len(current_locations) > 1:
        latest = current_locations[-1]
        deduped = [fact for fact in deduped if fact[0] != "person.location.current"]
        deduped.append(("person.location.current", latest))
    return deduped
