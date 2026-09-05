"""Unit tests for repository scanning + content classification."""
from __future__ import annotations

from mcp_kb.ingestion.repo_scanner import RepoScanner
from mcp_kb.models import ContentType


def test_classification(settings):
    scanner = RepoScanner(settings)
    assert scanner.classify("src/main/java/com/rx/A.java") == ContentType.JAVA
    assert scanner.classify("pom.xml") == ContentType.MAVEN
    assert scanner.classify(
        "src/main/resources/application.yml") == ContentType.SPRING_YAML
    assert scanner.classify(
        "src/main/resources/db/changelog/changelog-1.0.xml") == ContentType.LIQUIBASE
    assert scanner.classify("docs/incidents/INC-1.md") == ContentType.INCIDENTS
    assert scanner.classify("README.md") == ContentType.DOCS
    assert scanner.classify("target/classes/A.class") is None


def test_scan_discovers_all_types(settings, fixture_repo):
    scanner = RepoScanner(settings)
    found = list(scanner.scan("rx-order-service", fixture_repo))
    by_type = {f.content_type for f in found}
    assert ContentType.JAVA in by_type
    assert ContentType.MAVEN in by_type
    assert ContentType.SPRING_YAML in by_type
    assert ContentType.LIQUIBASE in by_type
    assert ContentType.INCIDENTS in by_type
    # Every scanned file carries a stable content hash.
    assert all(len(f.sha256) == 64 for f in found)
