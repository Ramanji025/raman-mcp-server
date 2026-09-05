"""Extractive inspection scorecard — graph facts only, no LLM.

Scores 0–100 per dimension and a letter grade so a weak model can present
the report without inventing findings.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 55:
        return "D"
    return "F"


class DimensionScore(BaseModel):
    """Score, grade, findings and recommended actions for one inspection dimension."""

    name: str
    score: float
    grade: str
    findings: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


class InspectionReport(BaseModel):
    """Full inspection-bible scorecard for a service or the whole application."""

    scope: str
    overall_score: float
    overall_grade: str
    dimensions: list[DimensionScore]
    services: list[str] = Field(default_factory=list)

    def as_markdown(self) -> str:
        """Render the full inspection report (all dimensions, findings, actions) as markdown."""
        lines = [
            f"# Inspection bible — `{self.scope}`",
            f"**Overall: {self.overall_grade} ({self.overall_score:.0f}/100)**",
            "",
            "| Dimension | Score | Grade |",
            "|---|---:|:---:|",
        ]
        for d in self.dimensions:
            lines.append(f"| {d.name} | {d.score:.0f} | {d.grade} |")
        for d in self.dimensions:
            lines += ["", f"## {d.name} ({d.grade})", ""]
            if d.findings:
                lines.append("**Findings**")
                lines.extend(f"- {f}" for f in d.findings[:12])
            if d.actions:
                lines.append("**Actions**")
                lines.extend(f"- {a}" for a in d.actions[:8])
        return "\n".join(lines) + "\n"


def score_security(snap: dict[str, Any]) -> DimensionScore:
    """Score the Security dimension: unprotected endpoints, hardcoded secrets, exception handling."""
    endpoints = max(int(snap.get("endpoint_count") or 0), 1)
    unprotected = int(snap.get("unprotected_count") or 0)
    secrets = int(snap.get("secret_count") or 0)
    has_advice = bool(snap.get("has_exception_handler"))
    penalty = min(80, unprotected * 8 + secrets * 20 + (0 if has_advice else 10))
    score = max(0.0, 100.0 - penalty)
    findings = []
    actions = []
    if unprotected:
        findings.append(f"{unprotected}/{endpoints} endpoints lack an auth annotation.")
        actions.append("Add @PreAuthorize or SecurityFilterChain rules on those paths.")
    if secrets:
        findings.append(f"{secrets} hardcoded secret(s) in config.")
        actions.append("Move secrets to env / vault.")
    if not has_advice:
        findings.append("No @ControllerAdvice / global exception handler detected.")
        actions.append("Add a central exception-to-HTTP mapper.")
    if not findings:
        findings.append("No high-severity security gaps in the graph snapshot.")
    return DimensionScore(name="Security", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def score_quality(snap: dict[str, Any]) -> DimensionScore:
    """Score the Code Quality dimension from detected antipattern severity counts."""
    high = int(snap.get("smells_high") or 0)
    med = int(snap.get("smells_medium") or 0)
    low = int(snap.get("smells_low") or 0)
    penalty = min(85, high * 12 + med * 5 + low * 1)
    score = max(0.0, 100.0 - penalty)
    findings = [f"{high} HIGH / {med} MEDIUM / {low} LOW antipatterns."]
    actions = []
    if high:
        actions.append("Fix HIGH smells first (N+1, missing @Valid, God class, secrets).")
    if not high and not med:
        findings.append("No HIGH/MEDIUM antipatterns recorded on graph nodes.")
    return DimensionScore(name="Code quality", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def score_coverage(snap: dict[str, Any]) -> DimensionScore:
    """Score the Test Coverage dimension from estimated class coverage percentage."""
    pct = float(snap.get("coverage_pct") or 0.0)
    untested_c = int(snap.get("untested_controllers") or 0)
    score = max(0.0, min(100.0, pct))
    findings = [f"Estimated class coverage {pct:.0f}%."]
    actions = []
    if untested_c:
        findings.append(f"{untested_c} controller(s) have no mapped test class.")
        actions.append("Add @WebMvcTest for untested controllers.")
    if pct < 80:
        actions.append("Raise coverage toward 80%+ on service/controller/repository types.")
    return DimensionScore(name="Test coverage", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def score_schema(snap: dict[str, Any]) -> DimensionScore:
    """Score the Data Schema dimension from entity antipatterns and migration coverage."""
    entities = int(snap.get("entity_count") or 0)
    ap = int(snap.get("schema_antipatterns") or 0)
    migrations = bool(snap.get("has_migrations"))
    penalty = min(70, ap * 10 + (0 if migrations or entities == 0 else 15))
    score = max(0.0, 100.0 - penalty)
    findings = [f"{entities} JPA entities, {ap} schema antipattern(s)."]
    actions = []
    if ap:
        actions.append("Review N+1 / missing-index smells on flagged entities.")
    if entities and not migrations:
        findings.append("No Flyway/Liquibase migration nodes detected.")
        actions.append("Confirm schema changes are versioned in migrations.")
    return DimensionScore(name="Data schema", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def score_events(snap: dict[str, Any]) -> DimensionScore:
    """Score the Events/Kafka dimension from unmatched producer/consumer topic counts."""
    unmatched_p = int(snap.get("unmatched_producers") or 0)
    unmatched_c = int(snap.get("unmatched_consumers") or 0)
    topics = int(snap.get("topic_count") or 0)
    penalty = min(80, unmatched_p * 15 + unmatched_c * 15)
    score = 100.0 if topics == 0 and unmatched_p == 0 else max(0.0, 100.0 - penalty)
    findings = [f"{topics} topic(s); {unmatched_p} unmatched producer(s), "
                f"{unmatched_c} unmatched consumer(s)."]
    actions = []
    if unmatched_p:
        actions.append("Add consumers or retire orphan produced topics.")
    if unmatched_c:
        actions.append("Add producers or drop orphan consumers.")
    return DimensionScore(name="Events / Kafka", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def score_architecture(snap: dict[str, Any]) -> DimensionScore:
    """Score the Architecture dimension from dependency fan-out and endpoint presence."""
    depends = int(snap.get("depends_on_count") or 0)
    dependents = int(snap.get("dependent_count") or 0)
    endpoints = int(snap.get("endpoint_count") or 0)
    # Fan-out above 8 is a coupling smell; isolated services with 0 endpoints also flagged.
    penalty = 0
    findings = [f"{endpoints} endpoints, depends on {depends}, consumed by {dependents}."]
    actions: list[str] = []
    if depends > 8:
        penalty += 20
        findings.append("High outbound coupling (>8 service dependencies).")
        actions.append("Split or facade chatty downstream calls.")
    if endpoints == 0:
        penalty += 15
        findings.append("No REST endpoints indexed for this service.")
    score = max(0.0, 100.0 - penalty)
    return DimensionScore(name="Architecture", score=score, grade=_grade(score),
                          findings=findings, actions=actions)


def build_report(scope: str, snap: dict[str, Any], *, services: list[str] | None = None) -> InspectionReport:
    """Score all inspection dimensions for one snapshot and assemble the full report."""
    dims = [
        score_security(snap),
        score_quality(snap),
        score_coverage(snap),
        score_schema(snap),
        score_events(snap),
        score_architecture(snap),
    ]
    overall = sum(d.score for d in dims) / len(dims)
    return InspectionReport(
        scope=scope,
        overall_score=round(overall, 1),
        overall_grade=_grade(overall),
        dimensions=dims,
        services=list(services or []),
    )


def merge_snaps(snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum numeric fields across per-service snapshots for an app-wide report."""
    keys = (
        "endpoint_count", "unprotected_count", "secret_count",
        "smells_high", "smells_medium", "smells_low",
        "untested_controllers", "entity_count", "schema_antipatterns",
        "unmatched_producers", "unmatched_consumers", "topic_count",
        "depends_on_count", "dependent_count",
    )
    out: dict[str, Any] = {k: 0 for k in keys}
    cov: list[float] = []
    advice = True
    migrations = False
    for s in snaps:
        for k in keys:
            out[k] += int(s.get(k) or 0)
        cov.append(float(s.get("coverage_pct") or 0.0))
        advice = advice and bool(s.get("has_exception_handler"))
        migrations = migrations or bool(s.get("has_migrations"))
    out["coverage_pct"] = sum(cov) / len(cov) if cov else 0.0
    out["has_exception_handler"] = advice
    out["has_migrations"] = migrations
    return out
