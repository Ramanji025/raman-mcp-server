"""Unit tests for the explainable severity scoring engine (rca/severity_scoring.py)."""
from __future__ import annotations

from mcp_kb.models import Severity
from mcp_kb.rca.severity_scoring import classify_severity


def test_high_impact_multi_service_is_p1():
    result = classify_severity(
        customer_impact="critical", revenue_impact="high",
        frequency_per_hour=150, services_affected=5,
        recovery_time_minutes=300, historical_pattern_similarity=0.9,
    )
    assert result.severity == Severity.P1
    assert result.confidence == 1.0
    assert len(result.factors) == 6


def test_no_inputs_defaults_to_p4_with_zero_confidence():
    result = classify_severity()
    assert result.severity == Severity.P4
    assert result.total_score == 0.0
    assert result.confidence == 0.0


def test_low_impact_single_service_fast_recovery_is_low_severity():
    result = classify_severity(
        customer_impact="low", services_affected=1, recovery_time_minutes=10,
    )
    assert result.severity in (Severity.P3, Severity.P4)


def test_every_factor_has_an_explainable_rationale():
    result = classify_severity(customer_impact="medium", frequency_per_hour=5)
    for factor in result.factors:
        assert factor.rationale
        assert isinstance(factor.points, float)


def test_markdown_rendering_includes_all_factors():
    result = classify_severity(customer_impact="high")
    md = result.as_markdown()
    assert "Severity: " in md
    for factor in result.factors:
        assert factor.name in md


def test_unrecognised_impact_level_is_safe_default():
    result = classify_severity(customer_impact="catastrophic-ish")
    factor = next(f for f in result.factors if f.name == "customer_impact")
    assert factor.points == 0.0
