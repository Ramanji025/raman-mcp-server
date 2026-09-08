"""Scan .csproj files for NuGet PackageReference elements."""
from __future__ import annotations

import hashlib
from pathlib import Path

from lxml import etree

from ...models import DependencyArtifact
from ...security.safe_xml import safe_parse
from . import feature_catalog as fc


def _artifact_id(package_id: str) -> str:
    key = f"nuget::{package_id}".lower()
    return "dep:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def scan(repos_root: Path) -> list[DependencyArtifact]:
    """Return one DependencyArtifact per unique NuGet package found."""
    seen: dict[str, DependencyArtifact] = {}

    for csproj in sorted(repos_root.rglob("*.csproj")):
        service = csproj.parents[1].name  # …/repos/<service>/…/proj.csproj
        try:
            root = safe_parse(csproj).getroot()
        except etree.XMLSyntaxError:
            continue

        # Collect target framework(s)
        tf_elems = root.findall(".//TargetFramework") + root.findall(".//TargetFrameworks")
        target_fw = ", ".join(e.text or "" for e in tf_elems if e.text)

        for ref in root.findall(".//PackageReference"):
            pkg = ref.get("Include") or ""
            version = ref.get("Version") or ""
            if not pkg:
                continue

            dep_id = _artifact_id(pkg)
            entry = fc.lookup(pkg)

            if dep_id not in seen:
                seen[dep_id] = DependencyArtifact(
                    id=dep_id,
                    ecosystem="nuget",
                    group="",           # NuGet has no groupId
                    artifact=pkg,
                    version=version,
                    scope="compile",
                    is_spring_starter=False,
                    is_internal=False,
                    features=list(entry.features) if entry else [],
                    concept_names=list(entry.concept_names) if entry else [],
                    services=[service],
                )
            else:
                da = seen[dep_id]
                if service not in da.services:
                    seen[dep_id] = da.model_copy(update={"services": da.services + [service]})

        # Synthesize a meta-artifact for the target framework itself
        if target_fw:
            fw_name = f"Microsoft.NETCore.App ({target_fw})"
            fw_id = _artifact_id(fw_name)
            if fw_id not in seen:
                seen[fw_id] = DependencyArtifact(
                    id=fw_id,
                    ecosystem="nuget",
                    group="Microsoft",
                    artifact=fw_name,
                    version=target_fw,
                    scope="compile",
                    features=["ASP.NET Core", ".NET Runtime"],
                    concept_names=[],
                    services=[service],
                )

    return list(seen.values())
