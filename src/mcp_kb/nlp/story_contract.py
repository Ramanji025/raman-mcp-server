"""Map user-story acceptance criteria onto graph endpoints, methods, and tests.

This is deterministic NLP + fuzzy matching — not an LLM invention of files.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .query_understand import link_entities, parse_acceptance_criteria, tokenize


class ClauseMapping(BaseModel):
    """One Given/When/Then clause mapped to the code that implements/tests it."""

    kind: str
    text: str
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    methods: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class StoryContract(BaseModel):
    """Full acceptance-criteria-to-code mapping for a user story."""

    clauses: list[ClauseMapping]
    files_to_touch: list[str]
    missing_tests: list[str]
    steps: list[dict[str, Any]]

    def as_markdown(self) -> str:
        """Render the acceptance-criteria-to-code mapping as markdown."""
        lines = ["## Acceptance criteria → code", ""]
        if not self.clauses:
            lines.append("_No Given/When/Then clauses parsed. Add AC in that form for a tighter map._")
            return "\n".join(lines)
        for c in self.clauses:
            lines.append(f"### {c.kind.title()}: {c.text}")
            if c.endpoints:
                lines.append("**Endpoints**")
                for e in c.endpoints:
                    lines.append(f"- `{e.get('http_method', '')} {e.get('path') or e.get('name')}` "
                                 f"({e.get('service')})")
            if c.methods:
                lines.append("**Methods**")
                for m in c.methods:
                    lines.append(f"- `{m.get('class', '')}.{m.get('name')}` (`{m.get('file', '')}`)")
            if c.tests:
                lines.append("**Tests**")
                for t in c.tests:
                    lines.append(f"- `{t.get('name')}` → `{t.get('tested_class', '')}`")
            if c.gaps:
                lines.append("**Gaps**")
                lines.extend(f"- {g}" for g in c.gaps)
            lines.append("")
        if self.files_to_touch:
            lines += ["## Files to touch"]
            lines.extend(f"- `{f}`" for f in self.files_to_touch)
            lines.append("")
        if self.missing_tests:
            lines += ["## Tests to write"]
            lines.extend(f"- {g}" for g in self.missing_tests)
        lines += ["", "## Graph implementation steps"]
        for i, step in enumerate(self.steps, start=1):
            lines.append(f"{i}. **{step.get('service', '?')}**: {step.get('action')}")
            for f in step.get("files") or []:
                lines.append(f"   - `{f}`")
        return "\n".join(lines) + "\n"


def _catalog_names(items: list[dict[str, Any]], key: str = "name") -> list[str]:
    return [str(i.get(key) or "") for i in items if i.get(key)]


def _pick(items: list[dict[str, Any]], query: str, *, limit: int = 3) -> list[dict[str, Any]]:
    names = _catalog_names(items)
    linked = link_entities(query, names, limit=limit)
    by_name = {str(i.get("name") or ""): i for i in items}
    out = []
    seen: set[str] = set()
    for name, _ in linked:
        row = by_name.get(name)
        if row and name not in seen:
            seen.add(name)
            out.append(row)
    if out:
        return out
    tokens = {t.lower() for t in tokenize(query)}
    scored: list[tuple[int, dict]] = []
    for item in items:
        blob = " ".join(str(item.get(k) or "") for k in ("name", "path", "class", "file")).lower()
        hit = sum(1 for t in tokens if len(t) > 3 and t in blob)
        if hit:
            scored.append((hit, item))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:limit]]


def build_story_contract(
    story_text: str,
    *,
    endpoints: list[dict[str, Any]],
    methods: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    retrieval_files: list[str] | None = None,
) -> StoryContract:
    """Map a user story's Given/When/Then clauses to endpoints, methods, and tests."""
    clauses_raw = parse_acceptance_criteria(story_text)
    if not clauses_raw:
        clauses_raw = [("when", story_text.strip()[:240])] if story_text.strip() else []

    mappings: list[ClauseMapping] = []
    files: list[str] = []
    missing_tests: list[str] = []

    for kind, text in clauses_raw:
        eps = _pick(endpoints, text)
        mths = _pick(methods, text)
        tsts = _pick(tests, text)
        clause_files = []
        for row in eps + mths:
            f = row.get("file") or ""
            if f:
                clause_files.append(f)
                files.append(f)
        gaps = []
        if kind in {"when", "then"} and not eps and not mths:
            gaps.append("No endpoint or method in the graph matched this clause.")
        if kind == "then" and not tsts:
            gap = f"No test class mapped for: {text[:80]}"
            gaps.append(gap)
            missing_tests.append(gap)
        mappings.append(ClauseMapping(
            kind=kind, text=text, endpoints=eps, methods=mths, tests=tsts,
            files=clause_files, gaps=gaps,
        ))

    if retrieval_files:
        for f in retrieval_files:
            if f:
                files.append(f)

    unique_files = list(dict.fromkeys(files))
    by_service: dict[str, list[str]] = {}
    for m in mappings:
        for row in m.endpoints + m.methods:
            svc = str(row.get("service") or "unknown")
            by_service.setdefault(svc, [])
            if row.get("file"):
                by_service[svc].append(row["file"])
    steps = []
    for svc, svc_files in by_service.items():
        uniq = list(dict.fromkeys(svc_files))
        steps.append({
            "service": svc,
            "action": f"Change {len(uniq) or 1} file(s) so AC clauses that mapped here still hold.",
            "files": uniq[:12],
        })
    if not steps and unique_files:
        steps.append({
            "service": "unknown",
            "action": "Review retrieved files; graph names did not match AC tokens closely.",
            "files": unique_files[:12],
        })
    return StoryContract(
        clauses=mappings,
        files_to_touch=unique_files[:30],
        missing_tests=list(dict.fromkeys(missing_tests)),
        steps=steps,
    )
