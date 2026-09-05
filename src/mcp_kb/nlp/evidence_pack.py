"""Extractive evidence pack: the answer the platform returns to a (possibly weak) LLM.

The pack is already a grounded report. An LLM may rephrase it but must not add
service names, APIs, tables, or root causes that are not in the pack.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..models import RetrievedChunk
from ..retrieval.hybrid_retriever import HybridResult

_SENT_SPLIT = re.compile(r"(?<=[.!?;\n])\s+")


class EvidenceFact(BaseModel):
    """One grounding fact (project/graph/technology/web) backing an answer."""

    text: str
    source: str
    score: float = 0.0
    kind: str = "project"  # project | graph | technology | web


class EvidencePack(BaseModel):
    """Extractive evidence bundle assembled for one question, with a confidence score."""

    question: str
    confidence: float
    facts: list[EvidenceFact] = Field(default_factory=list)
    graph_facts: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    web_facts: list[EvidenceFact] = Field(default_factory=list)
    intent: str = ""
    handler: str = ""

    def with_web(self, hits: list[dict[str, Any]]) -> EvidencePack:
        """Return a copy of this pack with web search hits added as EvidenceFacts."""
        web: list[EvidenceFact] = []
        for h in hits:
            snippet = (h.get("snippet") or h.get("body") or "").strip()
            url = h.get("url") or h.get("href") or ""
            title = h.get("title") or ""
            if not snippet:
                continue
            web.append(EvidenceFact(
                text=f"{title}: {snippet}"[:500],
                source=f"web:{url}",
                score=0.3,
                kind="web",
            ))
        gaps = [g for g in self.gaps if g != "insufficient_project_evidence"]
        return self.model_copy(update={"web_facts": web, "gaps": gaps})

    def as_markdown(self) -> str:
        """Render the evidence pack (facts, gaps, confidence) as markdown."""
        lines = [
            "# Evidence pack",
            f"**Question:** {self.question}",
            f"**Confidence:** {self.confidence:.0%} · intent `{self.intent}` → `{self.handler}`",
            "",
            "## Facts",
        ]
        if not self.facts:
            lines.append("_No project passages ranked high enough._")
        for i, f in enumerate(self.facts, start=1):
            lines.append(f"{i}. {f.text}  \n   _{f.source}_")
        if self.graph_facts:
            lines += ["", "## Graph"]
            lines.extend(f"- {g}" for g in self.graph_facts)
        if self.web_facts:
            lines += ["", "## External (web gap-fill)",
                      "_Not a source of internal service/API/table names._"]
            for w in self.web_facts:
                lines.append(f"- {w.text}  \n  _{w.source}_")
        if self.gaps:
            lines += ["", "## Gaps"]
            lines.extend(f"- {g}" for g in self.gaps)
        lines += ["", "## Sources"]
        seen: set[str] = set()
        for f in self.facts + self.web_facts:
            if f.source not in seen:
                seen.add(f.source)
                lines.append(f"- `{f.source}`")
        return "\n".join(lines) + "\n"


def _sentences(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = _SENT_SPLIT.split(raw)
    out = []
    for p in parts:
        s = " ".join(p.split())
        if 24 <= len(s) <= 420:
            out.append(s)
        elif len(s) > 420:
            out.append(s[:420].rsplit(" ", 1)[0])
    return out or [raw[:420]]


def _overlap(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9_@]+", text)}
    if not words:
        return 0.0
    return len(query_tokens & words) / len(query_tokens)


def build_evidence_pack(
    question: str,
    retrieval: HybridResult | None,
    *,
    graph_nodes: list[dict[str, Any]] | None = None,
    query_tokens: list[str] | None = None,
    intent: str = "",
    handler: str = "",
    tool_markdown: str | None = None,
) -> EvidencePack:
    """Assemble an extractive evidence pack (facts, gaps, confidence) from retrieval + graph."""
    q_tokens = {t.lower() for t in (query_tokens or re.findall(r"[A-Za-z0-9_@]+", question))}
    facts: list[EvidenceFact] = []

    if tool_markdown:
        for sent in _sentences(tool_markdown)[:8]:
            facts.append(EvidenceFact(text=sent, source="tool", score=0.7, kind="project"))

    chunks: list[RetrievedChunk] = list(retrieval.chunks) if retrieval else []
    for rc in chunks[:12]:
        loc = f"{rc.chunk.repo}/{rc.chunk.rel_path}"
        if rc.chunk.start_line:
            loc += f":{rc.chunk.start_line}"
        best = None
        best_s = -1.0
        for sent in _sentences(rc.chunk.text):
            s = 0.6 * float(rc.score) + 0.4 * _overlap(q_tokens, sent)
            if s > best_s:
                best_s, best = s, sent
        if best:
            facts.append(EvidenceFact(text=best, source=loc, score=round(best_s, 4)))

    facts.sort(key=lambda f: f.score, reverse=True)
    facts = facts[:10]

    nodes = graph_nodes if graph_nodes is not None else (
        list(retrieval.graph_nodes) if retrieval else []
    )
    graph_facts = []
    for n in nodes[:12]:
        if not n:
            continue
        graph_facts.append(
            f"{n.get('type') or 'Node'}: {n.get('name')} "
            f"(service={n.get('service') or '—'})"
        )

    gaps: list[str] = []
    if not facts:
        gaps.append("insufficient_project_evidence")
    if not graph_facts:
        gaps.append("no_graph_nodes")

    top = facts[0].score if facts else 0.0
    confidence = min(1.0, 0.3 * min(len(facts), 4) + 0.2 * min(len(graph_facts), 3)
                     + 0.35 * min(top, 1.0))
    if not facts:
        confidence = min(confidence, 0.25)

    return EvidencePack(
        question=question,
        confidence=round(confidence, 3),
        facts=facts,
        graph_facts=graph_facts,
        gaps=gaps,
        intent=intent,
        handler=handler,
    )
