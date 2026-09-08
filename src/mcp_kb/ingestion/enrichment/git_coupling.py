"""Git co-change coupling: files that historically change together.

Mirrors codebase-memory-mcp's `pass_githistory` (min-commits threshold,
coupling-score cutoff, refactor-noise filter) using GitPython (already a
project dependency via `ingestion/git_manager.py`). Surfaces hidden
architectural coupling that static import/call analysis misses.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from git import InvalidGitRepositoryError, Repo

from ...config import Settings
from ...graph.base import GraphStorePort
from ...ingestion.parsers.base import make_node_id
from ...logging import get_logger
from ...models import EdgeType, GraphEdge, GraphNode, NodeType

log = get_logger(__name__)

# Commits touching more files than this are treated as noisy refactors/
# formatting sweeps and excluded from coupling analysis.
_MAX_FILES_PER_COMMIT = 20


class GitCouplingEnrichment:
    """Computes FILE_CHANGES_WITH edges from git history for one repo."""

    def __init__(self, settings: Settings, graph: GraphStorePort) -> None:
        self._settings = settings
        self.graph = graph
        self._min_commits = settings.git_coupling_min_commits
        self._min_score = settings.git_coupling_min_score
        self._lookback_days = settings.git_coupling_lookback_days

    def run(self, repo_name: str, repo_dir: Path) -> int:
        """Compute and write FILE_CHANGES_WITH edges for one repo; return edge count."""
        try:
            repo = Repo(repo_dir)
        except InvalidGitRepositoryError:
            log.warning("git_coupling_not_a_repo", repo=repo_name)
            return 0

        pair_counts: Counter[tuple[str, str]] = Counter()
        file_change_counts: Counter[str] = Counter()
        since = f"--since={self._lookback_days}.days.ago" if self._lookback_days > 0 else None
        rev_args = [since] if since else []
        for commit in repo.iter_commits(*rev_args, max_count=20000):
            if not commit.parents:  # skip initial commit (no diff baseline)
                continue
            files = sorted(set(commit.stats.files.keys()))
            if len(files) > _MAX_FILES_PER_COMMIT or len(files) < 2:
                continue
            for f in files:
                file_change_counts[f] += 1
            for a, b in combinations(files, 2):
                pair_counts[(a, b)] += 1

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        seen_files: set[str] = set()
        for (a, b), co_changes in pair_counts.items():
            if co_changes < self._min_commits:
                continue
            denom = min(file_change_counts[a], file_change_counts[b]) or 1
            score = co_changes / denom
            if score < self._min_score:
                continue
            a_id = make_node_id("file", repo_name, a)
            b_id = make_node_id("file", repo_name, b)
            for fid, fpath in ((a_id, a), (b_id, b)):
                if fid not in seen_files:
                    seen_files.add(fid)
                    nodes.append(GraphNode(id=fid, type=NodeType.FILE, name=fpath,
                                          service=repo_name, attributes={"path": fpath}))
            edges.append(GraphEdge(src=a_id, dst=b_id, type=EdgeType.FILE_CHANGES_WITH,
                                   attributes={"co_changes": co_changes,
                                              "coupling_score": round(score, 3)}))

        if edges:
            self.graph.add_many(nodes, edges)
            self.graph.save()
        log.info("git_coupling_done", repo=repo_name, pairs_seen=len(pair_counts),
                 edges_created=len(edges))
        return len(edges)
