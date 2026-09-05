"""Unit tests for the first-class incident store (rca/incident_store.py),
JSON-backend path (no Postgres required)."""
from __future__ import annotations

from mcp_kb.models import IncidentRecord, Severity
from mcp_kb.rca.incident_store import IncidentStore


def test_save_and_get_roundtrip(settings):
    settings.postgres_enabled = False
    store = IncidentStore(settings)
    incident = IncidentRecord(
        id="", title="Order validation NPE", description="NPE in OrderService.validateOrder",
        severity=Severity.P2, service_name="rx-order", exception_type="NullPointerException",
        root_cause="missing null check", confidence=0.8, affected_services=["rx-order"],
    )
    saved = store.save(incident)
    assert saved.id  # auto-generated

    fetched = store.get(saved.id)
    assert fetched is not None
    assert fetched.title == "Order validation NPE"
    assert fetched.severity == Severity.P2


def test_list_filters_by_service_and_severity(settings):
    settings.postgres_enabled = False
    store = IncidentStore(settings)
    store.save(IncidentRecord(id="", title="A", severity=Severity.P1, service_name="rx-order"))
    store.save(IncidentRecord(id="", title="B", severity=Severity.P3, service_name="rx-pricing"))

    by_service = store.list(service="rx-order")
    assert len(by_service) == 1
    assert by_service[0].title == "A"

    by_severity = store.list(severity=Severity.P3)
    assert len(by_severity) == 1
    assert by_severity[0].title == "B"


def test_find_similar_keyword_fallback_without_retriever(settings):
    settings.postgres_enabled = False
    store = IncidentStore(settings)  # no retriever injected -> keyword fallback
    store.save(IncidentRecord(id="", title="Payment gateway timeout",
                              description="Timeout calling PaymentService under load",
                              severity=Severity.P2, service_name="rx-order"))
    store.save(IncidentRecord(id="", title="Unrelated disk space alert",
                              description="Disk usage exceeded 90%",
                              severity=Severity.P4, service_name="rx-infra"))

    results = store.find_similar("payment gateway timeout", top_k=5)
    assert results
    assert results[0]["title"] == "Payment gateway timeout"


def test_find_similar_returns_empty_when_no_incidents(settings):
    settings.postgres_enabled = False
    store = IncidentStore(settings)
    assert store.find_similar("anything") == []
