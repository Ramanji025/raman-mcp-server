"""Online learning from MCP usage — no GPU, no embedding retrain.

Signals
  * implicit: high-confidence answers boost cited chunks; a quick reformulation
    after a thin answer is a negative for the previous retrieval.
  * explicit: ``rate_answer`` (1–5).

Effects on the next ``ask``
  * query aliases (how this team names services)
  * extra expansion terms from similar past successes
  * retrieval score boosts on useful chunk ids / file paths
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_ALPHA = 0.25
_BOOST_CAP = 1.0
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_tokens(text: str) -> list[str]:
    """Lowercase-tokenize text for Jaccard similarity comparisons."""
    return [t.lower() for t in _TOKEN.findall(text or "")]


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    """Return the Jaccard similarity (intersection over union) of two token sets."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class InteractionEvent(BaseModel):
    """One logged ask/tool-call interaction, used for online learning signals."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: str = Field(default_factory=_now)
    question: str
    expanded: str = ""
    intent: str = ""
    handler: str = ""
    confidence: float = 0.0
    chunk_ids: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    rating: int | None = None
    implicit: str | None = None  # positive | negative | None
    note: str = ""


class LearningState(BaseModel):
    """Persisted online-learning weights: aliases, retrieval boosts, route win/loss counts."""

    aliases: dict[str, str] = Field(default_factory=dict)
    alias_counts: dict[str, int] = Field(default_factory=dict)
    chunk_boosts: dict[str, float] = Field(default_factory=dict)
    path_boosts: dict[str, float] = Field(default_factory=dict)
    route_wins: dict[str, int] = Field(default_factory=dict)
    model_boosts: dict[str, float] = Field(default_factory=dict)  # written by scripts/train_ranker.py
    route_losses: dict[str, int] = Field(default_factory=dict)
    success_queries: list[dict[str, Any]] = Field(default_factory=list)
    n_interactions: int = 0
    n_ratings: int = 0
    n_implicit_pos: int = 0
    n_implicit_neg: int = 0


class OnlineLearner:
    """Persist usage memory under ``data/learning/`` and apply it at query time."""

    def __init__(self, settings: Settings, *, root: Path | None = None) -> None:
        self._enabled = settings.learning_enabled
        self._settings = settings
        root = Path(root) if root is not None else Path(settings.repos_root).parent / "learning"
        root.mkdir(parents=True, exist_ok=True)
        self._events_path = root / "events.jsonl"
        self._state_path = root / "state.json"
        self._lock = threading.Lock()
        self._state = self._load_state()
        self._last: InteractionEvent | None = None

    @property
    def enabled(self) -> bool:
        """Whether online learning is enabled (LEARNING_ENABLED setting)."""
        return self._enabled

    def state(self) -> LearningState:
        """Return the current in-memory learning state."""
        return self._state

    def apply_aliases(self, question: str) -> str:
        """Rewrite `question` using learned term aliases (longest-alias-first)."""
        if not self._enabled or not question:
            return question
        out = question
        for src, dst in sorted(self._state.aliases.items(), key=lambda kv: -len(kv[0])):
            if len(src) < 4:
                continue
            pattern = re.compile(re.escape(src), re.IGNORECASE)
            if pattern.search(out) and dst.lower() not in out.lower():
                out = pattern.sub(dst, out)
        return out

    def extra_expansion_terms(self, question: str) -> list[str]:
        """Return extra query-expansion terms learned from similar past successful queries."""
        if not self._enabled:
            return []
        qtok = normalize_tokens(question)
        extras: list[str] = []
        for row in self._state.success_queries[-80:]:
            if jaccard(qtok, row.get("tokens") or []) >= 0.5:
                extras.extend(row.get("terms") or [])
        # de-dupe, keep order
        seen: set[str] = set()
        out = []
        for t in extras:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out[:8]

    def retrieval_boosts(self) -> dict[str, float]:
        """Merge LightGBM model boosts with heuristic chunk/path boosts for retrieval scoring."""
        if not self._enabled:
            return {}
        merged = dict(self._load_model_boosts())  # LightGBM base signal, if trained
        merged.update(self._state.chunk_boosts)  # heuristic overrides / refines
        merged.update(self._state.path_boosts)
        return merged

    def _load_model_boosts(self) -> dict[str, float]:
        """Lazily read scripts/train_ranker.py output (data/models/model_boosts.json)."""
        path = Path(self._settings.learning_model_path).with_name("model_boosts.json")
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("model_boosts_load_failed", error=str(exc))
            return {}

    def record_ask(
        self,
        *,
        question: str,
        expanded: str,
        intent: str,
        handler: str,
        confidence: float,
        chunk_ids: list[str],
        paths: list[str],
        entities: list[str],
    ) -> InteractionEvent:
        """Log one ask interaction and update learning weights from its implicit signal."""
        event = InteractionEvent(
            question=question, expanded=expanded, intent=intent, handler=handler,
            confidence=confidence, chunk_ids=chunk_ids[:20], paths=paths[:20],
            entities=entities[:20],
        )
        if not self._enabled:
            self._last = event
            return event
        with self._lock:
            implicit = self._infer_implicit(event)
            event.implicit = implicit
            self._append_event(event)
            self._update_from_event(event)
            self._last = event
            self._save_state()
        return event

    def rate(self, interaction_id: str, rating: int, note: str = "") -> InteractionEvent | None:
        """Apply an explicit 1-5 rating to a past interaction and update learning weights."""
        rating = max(1, min(5, int(rating)))
        with self._lock:
            event = self._find_event(interaction_id)
            if event is None:
                return None
            event.rating = rating
            event.note = note or event.note
            event.implicit = "positive" if rating >= 4 else ("negative" if rating <= 2 else event.implicit)
            self._append_event(event.model_copy(update={"note": f"rating:{rating} {note}"}))
            self._state.n_ratings += 1
            self._apply_signal(event, positive=rating >= 4, negative=rating <= 2, weight=1.0)
            if rating >= 4:
                self._remember_success(event)
                self._learn_aliases(event)
            self._save_state()
            return event

    def summary(self) -> dict[str, Any]:
        """Return a dashboard-friendly summary of learning state (counts, top boosts/aliases)."""
        s = self._state
        top_chunks = sorted(s.chunk_boosts.items(), key=lambda kv: -kv[1])[:8]
        top_aliases = sorted(s.alias_counts.items(), key=lambda kv: -kv[1])[:8]
        return {
            "enabled": self._enabled,
            "n_interactions": s.n_interactions,
            "n_ratings": s.n_ratings,
            "n_implicit_pos": s.n_implicit_pos,
            "n_implicit_neg": s.n_implicit_neg,
            "alias_count": len(s.aliases),
            "boosted_chunks": len(s.chunk_boosts),
            "top_aliases": [{"pair": k, "count": v} for k, v in top_aliases],
            "top_chunk_boosts": [{"id": k, "boost": round(v, 3)} for k, v in top_chunks],
            "last_interaction_id": self._last.id if self._last else None,
        }

    # ------------------------------------------------------------------ #
    def _infer_implicit(self, event: InteractionEvent) -> str | None:
        prev = self._last
        if prev is None:
            if event.confidence >= 0.6 and event.chunk_ids:
                return "positive"
            return None
        try:
            prev_ts = datetime.fromisoformat(prev.ts)
            age = (datetime.now(UTC) - prev_ts).total_seconds()
        except Exception:
            age = 9999
        sim = jaccard(normalize_tokens(prev.question), normalize_tokens(event.question))
        if 0 < age < 900 and 0.25 <= sim < 0.95 and prev.confidence < 0.45:
            # User rephrased a weak answer — punish previous retrieval.
            self._apply_signal(prev, positive=False, negative=True, weight=0.6)
            self._state.n_implicit_neg += 1
            self._state.route_losses[prev.handler] = self._state.route_losses.get(prev.handler, 0) + 1
        if event.confidence >= 0.6 and event.chunk_ids:
            return "positive"
        return None

    def _update_from_event(self, event: InteractionEvent) -> None:
        self._state.n_interactions += 1
        if event.implicit == "positive":
            self._state.n_implicit_pos += 1
            self._apply_signal(event, positive=True, negative=False, weight=0.5)
            self._remember_success(event)
            self._learn_aliases(event)
            self._state.route_wins[event.handler] = self._state.route_wins.get(event.handler, 0) + 1
        elif event.implicit == "negative":
            self._state.n_implicit_neg += 1
            self._apply_signal(event, positive=False, negative=True, weight=0.5)

    def _apply_signal(self, event: InteractionEvent, *, positive: bool, negative: bool,
                      weight: float) -> None:
        delta = _ALPHA * weight * (1.0 if positive else -0.7 if negative else 0.0)
        if delta == 0:
            return
        for cid in event.chunk_ids:
            cur = self._state.chunk_boosts.get(cid, 0.0)
            self._state.chunk_boosts[cid] = max(-0.4, min(_BOOST_CAP, cur + delta))
        for path in event.paths:
            cur = self._state.path_boosts.get(path, 0.0)
            self._state.path_boosts[path] = max(-0.4, min(_BOOST_CAP, cur + delta))

    def _remember_success(self, event: InteractionEvent) -> None:
        tokens = normalize_tokens(event.question)
        if not tokens:
            return
        row = {
            "tokens": tokens[:24],
            "terms": list(dict.fromkeys(event.entities + tokens[:6]))[:10],
            "ts": time.time(),
        }
        self._state.success_queries.append(row)
        self._state.success_queries = self._state.success_queries[-200:]

    def _learn_aliases(self, event: InteractionEvent) -> None:
        q = event.question.lower()
        for ent in event.entities:
            if not ent or len(ent) < 5:
                continue
            el = ent.lower()
            for tok in normalize_tokens(event.question):
                if 4 <= len(tok) <= 40 and tok in el and tok != el and tok in q:
                    prev = self._state.aliases.get(tok)
                    if prev and prev != ent:
                        continue
                    self._state.aliases[tok] = ent
                    key = f"{tok}|{ent}"
                    self._state.alias_counts[key] = self._state.alias_counts.get(key, 0) + 1

    def _append_event(self, event: InteractionEvent) -> None:
        try:
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        except Exception as exc:
            log.warning("learning_event_write_failed", error=str(exc))

    def _find_event(self, interaction_id: str) -> InteractionEvent | None:
        iid = (interaction_id or "last").strip().lower()
        if iid in {"", "last", "latest"}:
            return self._last
        if self._last and self._last.id == interaction_id:
            return self._last
        if not self._events_path.exists():
            return None
        try:
            lines = self._events_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        for line in reversed(lines[-500:]):
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("id") == interaction_id:
                return InteractionEvent.model_validate(data)
        return None

    def _load_state(self) -> LearningState:
        if not self._state_path.exists():
            return LearningState()
        try:
            return LearningState.model_validate_json(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("learning_state_load_failed", error=str(exc))
            return LearningState()

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                self._state.model_dump_json(indent=2), encoding="utf-8",
            )
        except Exception as exc:
            log.warning("learning_state_save_failed", error=str(exc))


def apply_retrieval_boosts(
    chunks: list,
    boosts: dict[str, float],
    cap: float = 0.5,
) -> list:
    """Multiply chunk scores by (1 + learned boost). Used by HybridRetriever."""
    if not boosts or not chunks:
        return chunks
    from ..models import RetrievedChunk

    out = []
    for rc in chunks:
        b = 0.0
        cid = getattr(getattr(rc, "chunk", None), "id", "") or ""
        path = getattr(getattr(rc, "chunk", None), "rel_path", "") or ""
        b += float(boosts.get(cid, 0.0))
        b += float(boosts.get(path, 0.0))
        b = max(-0.4, min(cap, b))
        out.append(RetrievedChunk(chunk=rc.chunk, score=rc.score * (1.0 + b),
                                  source=getattr(rc, "source", "fused")))
    out.sort(key=lambda x: x.score, reverse=True)
    return out
