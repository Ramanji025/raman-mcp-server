"""Staleness transparency for `ask()` answers.

Computes, per citation, how old the underlying source is (repo last-indexed
timestamp for project code, doc-harvest timestamp for technology facts, "live"
for web gap-fill) so a caller can see an answer may rest on stale data instead
of assuming everything is current.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..db.metadata_store import MetadataStore
from ..logging import get_logger

log = get_logger(__name__)

_STALE_AFTER_DAYS = 30


def _age_days(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - ts).days)
    except ValueError:
        return None


def compute_freshness(
    *,
    repos: list[str],
    web_used: bool,
    metadata: MetadataStore | None,
) -> dict[str, Any]:
    """Return {"sources": [...], "oldest_days": int|None, "stale": bool}."""
    sources: list[dict[str, Any]] = []

    repo_age_cache: dict[str, int | None] = {}
    for repo in repos or []:
        if not repo:
            continue
        if repo not in repo_age_cache:
            last_indexed = metadata.get_repo_last_indexed(repo) if metadata else None
            repo_age_cache[repo] = _age_days(last_indexed)
        sources.append({"kind": "project", "repo": repo, "age_days": repo_age_cache[repo]})

    if web_used:
        sources.append({"kind": "web", "name": "live search", "age_days": 0})

    known_ages = [s["age_days"] for s in sources if s["age_days"] is not None]
    oldest = max(known_ages) if known_ages else None
    return {
        "sources": sources,
        "oldest_days": oldest,
        "unknown_age_count": sum(1 for s in sources if s["age_days"] is None),
        "stale": bool(oldest is not None and oldest > _STALE_AFTER_DAYS),
    }


def freshness_markdown(freshness: dict[str, Any]) -> str:
    """Render a \"## Freshness\" markdown section, with a staleness warning if applicable."""
    if not freshness or not freshness.get("sources"):
        return ""
    lines = ["", "## Freshness"]
    oldest = freshness.get("oldest_days")
    if freshness.get("stale"):
        lines.append(f"⚠️ _Oldest source is {oldest} day(s) old — may be stale._")
    elif oldest is not None:
        lines.append(f"_Oldest source: {oldest} day(s) old._")
    if freshness.get("unknown_age_count"):
        lines.append(
            f"_{freshness['unknown_age_count']} source(s) have no tracked age "
            f"(technology concepts aren't timestamped end-to-end yet)._"
        )
    return "\n".join(lines) + "\n"
