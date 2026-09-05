"""Lexical ranking helpers used when Qdrant sparse BM25 is not yet backfilled."""
from __future__ import annotations

import re

from ..models import RetrievedChunk

_TERM = re.compile(r"[A-Za-z0-9_@./{}-]{2,}")


def tokenize(text: str) -> list[str]:
    """Split text into lowercased term tokens for lexical overlap scoring."""
    return [t.lower() for t in _TERM.findall(text or "")]


def lexical_rank(query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Order chunks by query-term overlap (BM25-like when sparse is unavailable)."""
    q = set(tokenize(query))
    if not q or not chunks:
        return list(chunks)

    scored: list[tuple[float, RetrievedChunk]] = []
    for rc in chunks:
        terms = tokenize(rc.chunk.text)
        if not terms:
            scored.append((0.0, rc))
            continue
        tf: dict[str, int] = {}
        for t in terms:
            tf[t] = tf.get(t, 0) + 1
        overlap = sum(1 for t in q if t in tf)
        score = overlap / len(q) + 0.1 * sum(tf.get(t, 0) for t in q) / max(len(terms), 1)
        scored.append((score, RetrievedChunk(
            chunk=rc.chunk, score=score, source="lexical",
        )))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [rc for _, rc in scored]


def should_use_web(
    *,
    enabled: bool,
    confidence: float,
    min_confidence: float,
) -> bool:
    """Gap-fill only: fire when pack confidence is below the configured floor."""
    if not enabled:
        return False
    return confidence < min_confidence
