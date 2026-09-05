"""Inspection scorecard math (no graph/Qdrant)."""
from __future__ import annotations

from mcp_kb.inspection.scorecard import build_report, merge_snaps, score_security


def test_security_penalizes_unprotected_and_secrets():
    d = score_security({
        "endpoint_count": 10, "unprotected_count": 4, "secret_count": 1,
        "has_exception_handler": False,
    })
    assert d.score < 70
    assert d.grade in {"C", "D", "F"}
    assert any("secret" in f.lower() for f in d.findings)


def test_healthy_snapshot_grades_well():
    snap = {
        "endpoint_count": 12, "unprotected_count": 0, "secret_count": 0,
        "has_exception_handler": True,
        "smells_high": 0, "smells_medium": 0, "smells_low": 1,
        "coverage_pct": 85, "untested_controllers": 0,
        "entity_count": 3, "schema_antipatterns": 0, "has_migrations": True,
        "unmatched_producers": 0, "unmatched_consumers": 0, "topic_count": 2,
        "depends_on_count": 2, "dependent_count": 1,
    }
    report = build_report("rx-order", snap, services=["rx-order"])
    assert report.overall_score >= 85
    assert report.overall_grade in {"A", "B"}
    md = report.as_markdown()
    assert "Inspection bible" in md
    assert "Security" in md


def test_merge_snaps_averages_coverage():
    merged = merge_snaps([
        {"coverage_pct": 80, "endpoint_count": 2, "has_exception_handler": True},
        {"coverage_pct": 40, "endpoint_count": 3, "has_exception_handler": False},
    ])
    assert merged["endpoint_count"] == 5
    assert merged["coverage_pct"] == 60
    assert merged["has_exception_handler"] is False
