"""Phase 2 graph-enrichment passes: cross-cutting analyses that run over the
*whole* already-populated knowledge graph (not per-file), mirroring
codebase-memory-mcp's post-extraction passes (similarity, semantic bridging,
git co-change coupling). Invoked as an extra build_all.py stage.
"""
from __future__ import annotations
