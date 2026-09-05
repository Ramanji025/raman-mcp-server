"""Explainable, hybrid rule-based severity classification (P1-P4).

Deliberately *not* an LLM call: severity that changes shape depending on a
model's mood is not auditable and cannot be reproduced during a post-mortem.
Every point on the scoreboard traces back to a named, weighted rule so an
engineer (or a compliance auditor) can see exactly why an incident was
scored P1 vs P2.

The LLM (when configured) may still *suggest* input values (e.g. "this looks
like it affects checkout, so customer_impact=high") but the arithmetic that
turns those inputs into P1-P4 is pure, deterministic Python.
"""
from __future__ import annotations

from ..models import Severity, SeverityFactor, SeverityScore

# ---- rubric --------------------------------------------------------------- #
_IMPACT_POINTS = {"none": 0.0, "low": 2.0, "medium": 5.0, "high": 8.0, "critical": 12.0}
_IMPACT_MAX = max(_IMPACT_POINTS.values())


def _impact_points(level: str | None) -> tuple[float, str]:
    if level is None:
        return 0.0, "not provided — assumed no measurable impact"
    key = level.strip().lower()
    if key not in _IMPACT_POINTS:
        return 0.0, f"unrecognised level '{level}' — assumed none"
    return _IMPACT_POINTS[key], f"reported as '{key}'"


def _frequency_points(events_per_hour: float | None) -> tuple[float, str]:
    if events_per_hour is None:
        return 0.0, "not provided"
    if events_per_hour >= 100:
        return 10.0, f"{events_per_hour:.0f}/hr — very high frequency"
    if events_per_hour >= 10:
        return 6.0, f"{events_per_hour:.0f}/hr — high frequency"
    if events_per_hour >= 1:
        return 3.0, f"{events_per_hour:.0f}/hr — moderate frequency"
    return 1.0, f"{events_per_hour:.2f}/hr — low frequency"


def _services_points(count: int | None) -> tuple[float, str]:
    if not count:
        return 0.0, "not provided / single unknown scope"
    if count >= 4:
        return 6.0, f"{count} services affected — wide blast radius"
    if count >= 2:
        return 3.0, f"{count} services affected"
    return 1.0, "1 service affected"


def _recovery_points(minutes: float | None) -> tuple[float, str]:
    if minutes is None:
        return 0.0, "not provided"
    if minutes >= 240:
        return 10.0, f"{minutes:.0f} min recovery — prolonged outage"
    if minutes >= 60:
        return 6.0, f"{minutes:.0f} min recovery"
    if minutes >= 15:
        return 3.0, f"{minutes:.0f} min recovery"
    return 1.0, f"{minutes:.0f} min recovery — fast mitigation"


def _historical_points(similarity: float | None) -> tuple[float, str]:
    if not similarity:
        return 0.0, "no matching historical pattern"
    if similarity >= 0.85:
        return 8.0, f"{similarity:.0%} match to a prior high-severity incident"
    if similarity >= 0.6:
        return 4.0, f"{similarity:.0%} match to a prior incident"
    return 1.0, f"{similarity:.0%} weak historical match"


_MAX_SCORE = _IMPACT_MAX + _IMPACT_MAX + 10.0 + 6.0 + 10.0 + 8.0  # = 58.0

# Thresholds are expressed as a fraction of the max score so the rubric can
# gain/lose factors later without re-deriving magic numbers by hand.
_P1_THRESHOLD = 0.55 * _MAX_SCORE
_P2_THRESHOLD = 0.35 * _MAX_SCORE
_P3_THRESHOLD = 0.15 * _MAX_SCORE


def classify_severity(
    *,
    customer_impact: str | None = None,
    revenue_impact: str | None = None,
    frequency_per_hour: float | None = None,
    services_affected: int | None = None,
    recovery_time_minutes: float | None = None,
    historical_pattern_similarity: float | None = None,
) -> SeverityScore:
    """Score an incident P1-P4 from hybrid, explainable inputs.

    Every argument is optional: missing data degrades the *confidence* of
    the result (fewer independent signals corroborating each other) rather
    than silently defaulting to a false sense of precision.
    """
    factors: list[SeverityFactor] = []
    provided = 0
    total = 0.0

    for name, weight, value, points_fn in (
        ("customer_impact", _IMPACT_MAX, customer_impact, _impact_points),
        ("revenue_impact", _IMPACT_MAX, revenue_impact, _impact_points),
    ):
        points, rationale = points_fn(value)
        factors.append(SeverityFactor(name=name, weight=weight, input_value=value,
                                       points=points, rationale=rationale))
        total += points
        provided += value is not None

    for name, weight, value, points_fn in (
        ("frequency_per_hour", 10.0, frequency_per_hour, _frequency_points),
        ("services_affected", 6.0, services_affected, _services_points),
        ("recovery_time_minutes", 10.0, recovery_time_minutes, _recovery_points),
        ("historical_pattern_similarity", 8.0, historical_pattern_similarity,
         _historical_points),
    ):
        points, rationale = points_fn(value)
        factors.append(SeverityFactor(name=name, weight=weight, input_value=value,
                                       points=points, rationale=rationale))
        total += points
        provided += value is not None

    if total >= _P1_THRESHOLD:
        severity = Severity.P1
    elif total >= _P2_THRESHOLD:
        severity = Severity.P2
    elif total >= _P3_THRESHOLD:
        severity = Severity.P3
    else:
        severity = Severity.P4

    confidence = provided / len(factors)
    return SeverityScore(severity=severity, total_score=round(total, 2),
                         max_score=_MAX_SCORE, factors=factors,
                         confidence=round(confidence, 2))
