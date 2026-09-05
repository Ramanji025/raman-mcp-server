"""Usage-driven online learning for routing and retrieval."""
from __future__ import annotations

from ..config import Settings
from .online import OnlineLearner, apply_retrieval_boosts

__all__ = ["OnlineLearner", "apply_retrieval_boosts", "get_learner"]


def get_learner(settings: Settings) -> OnlineLearner:
    """Return the configured learner backend (file by default; neo4j_qdrant opt-in)."""
    if settings.learning_backend == "neo4j_qdrant":
        from .graph_store import GraphOnlineLearner

        return GraphOnlineLearner(settings)
    return OnlineLearner(settings)
