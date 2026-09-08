"""MinHash + LSH near-duplicate detection over Method node bodies.

Mirrors codebase-memory-mcp's `pass_similarity` (MinHash K=16, banded LSH,
Jaccard-over-signature threshold) using pure Python — no new dependency.
Produces `SIMILAR_TO` edges between methods with near-identical body-token
shingle sets (candidates for refactoring/dedup), independent of language.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from ...config import Settings
from ...graph.base import GraphStorePort
from ...logging import get_logger
from ...models import EdgeType, GraphEdge, NodeType

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_MERSENNE = (1 << 61) - 1


def _tokenize(node: dict) -> set[str]:
    """Return the identifier shingle set for one Method node.

    Prefers the AST-derived `body_tokens` attribute (walked over real
    tree-sitter identifier nodes at parse time — see `java_parser.
    _extract_body_identifier_tokens`), which excludes string/comment
    literals and is exact per-language. Falls back to a regex scan of the
    stored `body` text only for parsers that don't populate `body_tokens`
    yet (pre-Phase-4 non-Java content types).
    """
    attrs = node.get("attributes") or {}
    precomputed = attrs.get("body_tokens")
    if precomputed:
        return set(precomputed.split())
    body = attrs.get("body") or node.get("body") or ""
    return {t.lower() for t in _TOKEN_RE.findall(body)}


def _stable_hash(token: str) -> int:
    """Deterministic 64-bit token hash (blake2b), stable across process
    restarts/PYTHONHASHSEED — unlike Python's built-in `hash()`, which is
    salted per-process (PEP 456) and would make MinHash signatures
    non-reproducible between an ingestion run and a later similarity re-run.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & _MERSENNE


class MinHasher:
    """K-permutation MinHash signature generator (K seeds, no external deps)."""

    def __init__(self, k: int = 32, seed: int = 1) -> None:
        self.k = k
        # Deterministic per-run multiplier/offset pairs (odd multipliers → full period).
        self._a = [((seed + i) * 2654435761 + 1) % _MERSENNE or 1 for i in range(k)]
        self._b = [((seed + i) * 40503 + 7) % _MERSENNE for i in range(k)]

    def signature(self, tokens: set[str]) -> tuple[int, ...]:
        """Return the K-value MinHash signature for a token set."""
        if not tokens:
            return tuple(0 for _ in range(self.k))
        hashes = [_stable_hash(t) for t in tokens]
        sig = []
        for a, b in zip(self._a, self._b):
            sig.append(min((a * h + b) % _MERSENNE for h in hashes))
        return tuple(sig)


def _lsh_bands(sig: tuple[int, ...], bands: int, rows: int) -> list[tuple[int, ...]]:
    return [sig[i * rows:(i + 1) * rows] for i in range(bands)]


def _approx_jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    if not sig_a or not sig_b:
        return 0.0
    matches = sum(1 for x, y in zip(sig_a, sig_b) if x == y)
    return matches / len(sig_a)


class SimilarityEnrichment:
    """Computes SIMILAR_TO edges across all indexed Method nodes."""

    def __init__(self, settings: Settings, graph: GraphStorePort) -> None:
        self._settings = settings
        self.graph = graph
        self._k = settings.similarity_minhash_k
        self._threshold = settings.similarity_jaccard_threshold
        self._bands = settings.similarity_lsh_bands
        self._rows = max(1, self._k // self._bands)

    def run(self, service: str | None = None) -> int:
        """Compute and write SIMILAR_TO edges; return the number of edges created."""
        methods = self.graph.nodes_by_type(NodeType.METHOD, service=service)
        hasher = MinHasher(k=self._k)
        signatures: dict[str, tuple[int, ...]] = {}
        min_tokens = 8  # skip trivial/getter-sized bodies (too many false positives)
        for node in methods:
            tokens = _tokenize(node)
            if len(tokens) < min_tokens:
                continue
            signatures[node["id"]] = hasher.signature(tokens)

        buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
        for node_id, sig in signatures.items():
            for band_idx, band in enumerate(_lsh_bands(sig, self._bands, self._rows)):
                buckets[(band_idx, band)].append(node_id)

        seen_pairs: set[tuple[str, str]] = set()
        edges: list[GraphEdge] = []
        for candidates in buckets.values():
            if len(candidates) < 2 or len(candidates) > 200:  # skip degenerate mega-buckets
                continue
            for i in range(len(candidates)):
                for j in range(i + 1, len(candidates)):
                    a, b = sorted((candidates[i], candidates[j]))
                    if (a, b) in seen_pairs or a == b:
                        continue
                    seen_pairs.add((a, b))
                    score = _approx_jaccard(signatures[a], signatures[b])
                    if score >= self._threshold:
                        edges.append(GraphEdge(src=a, dst=b, type=EdgeType.SIMILAR_TO,
                                               attributes={"jaccard": round(score, 3)}))
        if edges:
            self.graph.add_many([], edges)
            self.graph.save()
        log.info("similarity_enrichment_done", methods_scanned=len(methods),
                 candidates_fingerprinted=len(signatures), edges_created=len(edges))
        return len(edges)
