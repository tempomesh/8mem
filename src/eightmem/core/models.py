from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ChatMessage:
    role: str
    text: str


@dataclass
class Signals:
    identity: set[str] = field(default_factory=set)
    beliefs: set[str] = field(default_factory=set)
    preferences: set[str] = field(default_factory=set)
    corrections: set[str] = field(default_factory=set)
    evolution: set[str] = field(default_factory=set)
    decisions: set[str] = field(default_factory=set)

    def as_file_map(self) -> dict[str, set[str]]:
        return {
            "IDENTITY.md": self.identity,
            "BELIEFS.md": self.beliefs,
            "PREFERENCES.md": self.preferences,
            "CORRECTIONS.md": self.corrections,
            "EVOLUTION.md": self.evolution,
            "DECISIONS.md": self.decisions,
        }


@dataclass
class ImportChunk:
    text: str
    chunk_idx: int
    id: str = field(default_factory=lambda: str(uuid4()))
    word_count: int = 0


@dataclass
class ImportFactCandidate:
    file_name: str
    text: str
    confidence: float = 0.0
    source_chunk_id: str | None = None


@dataclass
class ImportDocument:
    source_kind: str
    source_id: str
    text: str
    chunks: list[ImportChunk] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid4()))
