"""Build a transitive dependency graph within the workspace.

For internal artifacts (groupId matches known prefixes) we can resolve full
transitive chains by cross-referencing every pom.xml.  For external artifacts
we emit TRANSITIVE_DEP edges only when the artifact itself is also declared in
another repo's pom.xml (i.e. it is part of the scanned workspace).

Algorithm
---------
1. Build ``direct_deps``: service → set of artifact ids it directly declares.
2. Build ``artifact_deps``: artifact id → set of artifact ids that artifact
   itself declares (from its own pom.xml, if found in the workspace).
3. BFS / DFS from each service through ``artifact_deps`` to enumerate
   transitive closure up to ``max_depth`` hops.

Returns a list of (src_artifact_id, dst_artifact_id) pairs representing
transitive dependency edges to add to the graph.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from lxml import etree

from ...models import DependencyArtifact

_MVN_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
_INTERNAL_GROUPS = ("com.bh", "com.waygate", "com.bhge", "com.baker-hughes")


def _text(elem, tag: str) -> str:
    for child in elem:
        local = etree.QName(child).localname if child.tag is not etree.Comment else ""
        if local == tag:
            return (child.text or "").strip()
    return ""


def resolve(
    repos_root: Path,
    artifacts: list[DependencyArtifact],
    max_depth: int = 3,
) -> list[tuple[str, str]]:
    """Return (src_id, dst_id) pairs for transitive dep edges."""
    # Index canonical artifacts by (group, artifact) for fast lookup
    by_coord: dict[tuple[str, str], str] = {
        (da.group.lower(), da.artifact.lower()): da.id for da in artifacts
    }

    # Build artifact → its own declared deps (from workspace pom.xml files)
    artifact_own_deps: dict[str, set[str]] = defaultdict(set)
    for pom_path in repos_root.rglob("pom.xml"):
        try:
            root = etree.parse(pom_path).getroot()
        except etree.XMLSyntaxError:
            continue

        # Identify which canonical artifact this pom.xml represents
        group = _text(root, "groupId")
        artifact = _text(root, "artifactId")
        if not artifact:
            continue
        src_id = by_coord.get((group.lower(), artifact.lower()))
        if src_id is None:
            continue

        # Extract its direct dependencies
        dep_elems = (root.findall(".//m:dependencies/m:dependency", _MVN_NS) or
                     root.findall(".//dependencies/dependency"))
        for dep in dep_elems:
            d_group = _text(dep, "groupId")
            d_art = _text(dep, "artifactId")
            dst_id = by_coord.get((d_group.lower(), d_art.lower()))
            if dst_id and dst_id != src_id:
                artifact_own_deps[src_id].add(dst_id)

    # BFS: for each artifact that has internal sub-deps, emit transitive edges
    edges: set[tuple[str, str]] = set()
    for root_id, direct_set in artifact_own_deps.items():
        visited = {root_id}
        queue: deque[tuple[str, int]] = deque((d, 1) for d in direct_set)
        while queue:
            node_id, depth = queue.popleft()
            if node_id in visited or depth > max_depth:
                continue
            visited.add(node_id)
            if depth > 1:  # depth==1 are direct; only emit depth>1 as TRANSITIVE
                edges.add((root_id, node_id))
            for child_id in artifact_own_deps.get(node_id, set()):
                if child_id not in visited:
                    queue.append((child_id, depth + 1))

    return list(edges)
