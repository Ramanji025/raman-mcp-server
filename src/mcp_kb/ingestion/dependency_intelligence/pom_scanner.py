"""Scan all pom.xml files in the repos root and produce DependencyArtifact records.

Reuses the lxml parsing helpers from the existing PomParser but operates at
the workspace level rather than the per-file level, building canonical
cross-service artifacts.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from lxml import etree

from ...models import DependencyArtifact
from ...security.safe_xml import safe_parse
from . import feature_catalog as fc

_MVN_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# Internal group-ID prefixes (workspace-specific — extend as needed)
_INTERNAL_GROUPS = ("com.bh", "com.waygate", "com.bhge", "com.baker-hughes")


def _text(elem, tag: str) -> str:
    for child in elem:
        local = etree.QName(child).localname if child.tag is not etree.Comment else ""
        if local == tag:
            return (child.text or "").strip()
    return ""


def _all_dep_elems(root):
    return (root.findall(".//m:dependencies/m:dependency", _MVN_NS) or
            root.findall(".//dependencies/dependency"))


def _artifact_id(group: str, artifact: str) -> str:
    key = f"maven:{group}:{artifact}".lower()
    return "dep:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def scan(repos_root: Path) -> list[DependencyArtifact]:
    """Return one DependencyArtifact per unique (groupId, artifactId) found."""
    seen: dict[str, DependencyArtifact] = {}  # keyed by sha id

    for pom_path in sorted(repos_root.rglob("pom.xml")):
        service = pom_path.parent.name
        try:
            root = safe_parse(pom_path).getroot()
        except etree.XMLSyntaxError:
            continue

        for dep in _all_dep_elems(root):
            group = _text(dep, "groupId")
            artifact = _text(dep, "artifactId")
            version = _text(dep, "version") or "managed"
            scope = _text(dep, "scope") or "compile"
            optional = _text(dep, "optional") == "true"

            if not artifact:
                continue

            dep_id = _artifact_id(group, artifact)
            entry = fc.lookup(artifact)

            if dep_id not in seen:
                seen[dep_id] = DependencyArtifact(
                    id=dep_id,
                    ecosystem="maven",
                    group=group,
                    artifact=artifact,
                    version=version,
                    scope=scope,
                    optional=optional,
                    is_spring_starter=artifact.startswith("spring-boot-starter"),
                    is_internal=any(group.startswith(p) for p in _INTERNAL_GROUPS),
                    features=list(entry.features) if entry else [],
                    concept_names=list(entry.concept_names) if entry else [],
                    services=[service],
                )
            else:
                da = seen[dep_id]
                if service not in da.services:
                    da.services.append(service)
                # Take most specific (non-managed) version seen
                if da.version == "managed" and version != "managed":
                    object.__setattr__(da, "version", version) if hasattr(da, "__dataclass_fields__") else None
                    seen[dep_id] = da.model_copy(update={"version": version})

    return list(seen.values())
