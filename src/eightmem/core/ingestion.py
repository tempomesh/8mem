from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from eightmem.analyzers.extractor import extract_signals
from eightmem.core.import_cache import create_import_cache_key, load_import_cache, save_import_cache
from eightmem.core.models import ChatMessage, ImportChunk, ImportDocument, ImportFactCandidate


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\S+")


def chunk_import_text(text: str, *, max_words: int = 220, overlap_words: int = 30) -> list[ImportChunk]:
    stripped = text.strip()
    if not stripped:
        return []

    words = _WORD_RE.findall(stripped)
    if len(words) <= max_words:
        return [ImportChunk(text=stripped, chunk_idx=0, word_count=len(words))]

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip()]
    if not sentences:
        return [ImportChunk(text=stripped, chunk_idx=0, word_count=len(words))]

    chunks: list[ImportChunk] = []
    current_sentences: list[str] = []
    current_words = 0

    def flush_chunk() -> None:
        nonlocal current_sentences, current_words
        if not current_sentences:
            return
        chunk_text = " ".join(current_sentences).strip()
        chunk_word_count = len(_WORD_RE.findall(chunk_text))
        chunks.append(
            ImportChunk(
                text=chunk_text,
                chunk_idx=len(chunks),
                word_count=chunk_word_count,
            )
        )
        if overlap_words <= 0:
            current_sentences = []
            current_words = 0
            return
        overlap: list[str] = []
        overlap_count = 0
        for sentence in reversed(current_sentences):
            sentence_words = len(_WORD_RE.findall(sentence))
            overlap.insert(0, sentence)
            overlap_count += sentence_words
            if overlap_count >= overlap_words:
                break
        current_sentences = overlap
        current_words = overlap_count

    for sentence in sentences:
        sentence_words = len(_WORD_RE.findall(sentence))
        if current_sentences and current_words + sentence_words > max_words:
            flush_chunk()
        current_sentences.append(sentence)
        current_words += sentence_words
    flush_chunk()
    return chunks


def build_import_document(
    source_text: str,
    *,
    source_kind: str,
    source_id: str,
    max_words: int = 220,
    overlap_words: int = 30,
) -> ImportDocument:
    chunks = chunk_import_text(source_text, max_words=max_words, overlap_words=overlap_words)
    return ImportDocument(
        source_kind=source_kind,
        source_id=source_id,
        text=source_text,
        chunks=chunks,
    )


def prepare_import_document(
    source_text: str,
    *,
    source_kind: str,
    source_id: str,
    max_words: int = 220,
    overlap_words: int = 30,
    use_cache: bool = True,
) -> tuple[ImportDocument, bool]:
    options = {"max_words": max_words, "overlap_words": overlap_words}
    key = create_import_cache_key(
        source_text,
        source_kind=source_kind,
        source_id=source_id,
        options=options,
    )
    if use_cache:
        cached = load_import_cache(key)
        if cached:
            chunks = [
                ImportChunk(
                    text=item["text"],
                    chunk_idx=item["chunk_idx"],
                    id=item.get("id") or "",
                    word_count=item.get("word_count", 0),
                )
                for item in cached.get("chunks", [])
            ]
            document = ImportDocument(
                source_kind=cached.get("source_kind", source_kind),
                source_id=cached.get("source_id", source_id),
                text=cached.get("text", source_text),
                chunks=chunks,
                id=cached.get("id") or "",
            )
            return document, True

    document = build_import_document(
        source_text,
        source_kind=source_kind,
        source_id=source_id,
        max_words=max_words,
        overlap_words=overlap_words,
    )
    if use_cache:
        save_import_cache(
            key,
            {
                "id": document.id,
                "source_kind": document.source_kind,
                "source_id": document.source_id,
                "text": document.text,
                "chunks": [asdict(chunk) for chunk in document.chunks],
            },
        )
    return document, False


def build_import_preview(document: ImportDocument) -> dict[str, Any]:
    return {
        "document_id": document.id,
        "source_kind": document.source_kind,
        "source_id": document.source_id,
        "chunk_count": len(document.chunks),
        "total_words": len(_WORD_RE.findall(document.text)),
        "chunks": [
            {
                "chunk_idx": chunk.chunk_idx,
                "word_count": chunk.word_count,
                "preview": chunk.text[:180],
            }
            for chunk in document.chunks
        ],
    }


def extract_import_fact_candidates(document: ImportDocument) -> list[ImportFactCandidate]:
    seen: dict[tuple[str, str], ImportFactCandidate] = {}
    for chunk in document.chunks:
        signals = extract_signals([ChatMessage(role="unknown", text=chunk.text)])
        for file_name, values in signals.as_file_map().items():
            for value in values:
                key = (file_name, value)
                if key in seen:
                    candidate = seen[key]
                    candidate.confidence = min(1.0, candidate.confidence + 0.1)
                    continue
                seen[key] = ImportFactCandidate(
                    file_name=file_name,
                    text=value,
                    confidence=0.6,
                    source_chunk_id=chunk.id,
                )
    return sorted(seen.values(), key=lambda item: (item.file_name, item.text.lower()))


def prepare_import_candidates(
    source_text: str,
    *,
    source_kind: str,
    source_id: str,
    max_words: int = 220,
    overlap_words: int = 30,
    use_cache: bool = True,
) -> tuple[ImportDocument, list[ImportFactCandidate], bool]:
    document, from_cache = prepare_import_document(
        source_text,
        source_kind=source_kind,
        source_id=source_id,
        max_words=max_words,
        overlap_words=overlap_words,
        use_cache=use_cache,
    )
    candidates = extract_import_fact_candidates(document)
    return document, candidates, from_cache


def build_candidate_review(candidates: list[ImportFactCandidate]) -> dict[str, list[dict[str, Any]]]:
    review: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        review.setdefault(candidate.file_name, []).append(
            {
                "text": candidate.text,
                "confidence": round(candidate.confidence, 2),
                "source_chunk_id": candidate.source_chunk_id,
            }
        )
    return review
