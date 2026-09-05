"""Maven ``pom.xml`` parser: service identity, full dependency tree, Spring Boot version."""
from __future__ import annotations

from lxml import etree

from ...models import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    ParseResult,
    SourceFile,
)
from .base import Parser, make_node_id

_MVN_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# Libraries known to have had high-severity CVEs — flag for review.
_CVE_WATCHLIST: dict[str, str] = {
    "log4j-core": "CVE-2021-44228 (Log4Shell) — upgrade to ≥2.17.1",
    "log4j": "CVE-2021-44228 family — prefer logback/log4j2 ≥2.17.1",
    "spring-core": "check for CVE-2022-22965 (Spring4Shell) if JDK9+ + WAR",
    "spring-security-oauth2": "CVE-2022-22969 — migrate to spring-authorization-server",
    "jackson-databind": "numerous CVEs in <2.13 — keep ≥2.14",
    "commons-collections": "CVE-2015-4852 — avoid versions 3.x/4.0 without deserialization protection",
    "commons-text": "CVE-2022-42889 (Text4Shell) — upgrade to ≥1.10",
    "h2": "CVE-2021-42392 — do not expose H2 console in production",
    "snakeyaml": "CVE-2022-25857 — upgrade to ≥1.32",
    "netty-codec": "multiple CVEs — keep ≥4.1.77",
}


class PomParser(Parser):
    """Parses Maven pom.xml files into dependency graph nodes."""

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse a Maven pom.xml file into dependency graph nodes."""
        result = ParseResult()
        service = self._service_name(source)
        try:
            tree = etree.parse(source.abs_path)
        except etree.XMLSyntaxError:
            return result
        root = tree.getroot()

        artifact = self._text(root, "artifactId") or service
        version  = self._text(root, "version")
        java_ver = self._text(root, "java.version") or self._prop(root, "java.version")
        sb_ver   = self._spring_boot_version(root)

        svc_id = make_node_id("service", service, service)
        result.nodes.append(
            GraphNode(
                id=svc_id, type=NodeType.SERVICE, name=service, service=service,
                attributes={
                    "artifactId": artifact, "version": version,
                    "spring_boot_version": sb_ver, "java_version": java_ver,
                    "build": "maven", "file": source.rel_path,
                },
            )
        )

        # Full dependency extraction.
        all_deps: list[dict] = []
        for dep in self._all_deps(root):
            dep_artifact = self._text(dep, "artifactId")
            group        = self._text(dep, "groupId")
            dep_ver      = self._text(dep, "version")
            scope        = self._text(dep, "scope") or "compile"
            optional     = self._text(dep, "optional") == "true"
            cve_note     = _CVE_WATCHLIST.get(dep_artifact, "")

            if not dep_artifact:
                continue

            dep_id = make_node_id("dependency", service, f"{group}:{dep_artifact}")
            dep_info = {
                "groupId": group, "artifactId": dep_artifact,
                "version": dep_ver or "managed", "scope": scope,
                "optional": optional, "cve_note": cve_note,
            }
            result.nodes.append(
                GraphNode(id=dep_id, type=NodeType.DEPENDENCY, name=dep_artifact,
                          service=service, attributes=dep_info)
            )
            result.edges.append(
                GraphEdge(src=svc_id, dst=dep_id, type=EdgeType.USES_DEPENDENCY,
                          attributes={"scope": scope})
            )
            all_deps.append(dep_info)

            # Cross-service dependency (ecosystem heuristic).
            if dep_artifact.startswith(("rx-", "rx_")) or "rx" in (group or ""):
                result.edges.append(
                    GraphEdge(src=svc_id,
                              dst=make_node_id("service", dep_artifact, dep_artifact),
                              type=EdgeType.DEPENDS_ON,
                              attributes={"groupId": group, "scope": "maven"})
                )

        # Attach full dep list to service node for tool queries.
        for n in result.nodes:
            if n.id == svc_id:
                n.attributes["dependencies"] = all_deps
                n.attributes["cve_flagged"] = [d for d in all_deps if d["cve_note"]]
                break

        result.chunks.extend(
            self._chunk_text(source, self._read(source), collection="architecture",
                             metadata={"artifact": "pom.xml", "service": service,
                                       "spring_boot_version": sb_ver})
        )
        return result

    # ------------------------------------------------------------------ #
    def _spring_boot_version(self, root) -> str | None:
        """Extract Spring Boot version from parent or dependency management."""
        parent = None
        for child in root:
            if not isinstance(child.tag, str):
                continue  # skip comments/PIs, which have non-string tags
            if etree.QName(child).localname == "parent":
                parent = child
                break
        if parent is not None:
            artifact = self._text(parent, "artifactId")
            if "spring-boot" in (artifact or ""):
                return self._text(parent, "version")
        # Fallback: look for spring-boot-dependencies in depMgmt.
        for dep in root.findall(".//dependencyManagement//dependency") + \
                   root.findall(".//m:dependencyManagement//m:dependency", _MVN_NS):
            if "spring-boot" in (self._text(dep, "artifactId") or ""):
                v = self._text(dep, "version")
                if v:
                    return v
        return None

    def _prop(self, root, key: str) -> str | None:
        for props in root.findall(".//properties") + root.findall(".//m:properties", _MVN_NS):
            for child in props:
                if etree.QName(child).localname == key:
                    return (child.text or "").strip()
        return None

    @staticmethod
    def _all_deps(root) -> list:
        deps = (root.findall(".//m:dependencies/m:dependency", _MVN_NS) or
                root.findall(".//dependencies/dependency"))
        return deps

    @staticmethod
    def _text(elem, tag: str) -> str:
        for child in elem:
            local = etree.QName(child).localname if child.tag is not etree.Comment else ""
            if local == tag:
                return (child.text or "").strip()
        return ""
