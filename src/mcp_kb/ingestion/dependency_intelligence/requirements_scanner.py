"""Scan requirements.txt and pyproject.toml files for Python dependencies."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ...models import DependencyArtifact
from . import feature_catalog as fc

# e.g. "boto3==1.28.0", "Pillow>=9.0", "requests", "# comment"
_REQ_LINE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(?:[=><!~^]+.*)?$")


def _artifact_id(package: str) -> str:
    key = f"pip::{package}".lower()
    return "dep:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _parse_requirements_txt(path: Path) -> list[tuple[str, str]]:
    """Return (package, version) pairs from a requirements.txt."""
    results: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _REQ_LINE.match(line)
        if m:
            pkg = m.group(1)
            version = line[len(pkg):].strip().lstrip("=><!~^").strip()
            results.append((pkg, version))
    return results


def _parse_pyproject_toml(path: Path) -> list[tuple[str, str]]:
    """Return (package, version) pairs from pyproject.toml [tool.poetry.dependencies]."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return []

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    deps: dict = {}
    # PEP 517 / setuptools style
    deps.update(data.get("project", {}).get("dependencies", {}))
    # Poetry style
    deps.update(data.get("tool", {}).get("poetry", {}).get("dependencies", {}))

    results: list[tuple[str, str]] = []
    for pkg, ver in deps.items():
        if pkg.lower() == "python":
            continue
        version = ver if isinstance(ver, str) else ""
        results.append((pkg, version))
    return results


def scan(repos_root: Path) -> list[DependencyArtifact]:
    """Scan all requirements.txt/pyproject.toml files under `repos_root` for Python dependencies."""
    seen: dict[str, DependencyArtifact] = {}

    for req_file in sorted(repos_root.rglob("requirements*.txt")):
        service = req_file.parents[1].name
        for pkg, version in _parse_requirements_txt(req_file):
            dep_id = _artifact_id(pkg)
            entry = fc.lookup(pkg)
            if dep_id not in seen:
                seen[dep_id] = DependencyArtifact(
                    id=dep_id,
                    ecosystem="pip",
                    group="",
                    artifact=pkg,
                    version=version,
                    scope="compile",
                    features=list(entry.features) if entry else [],
                    concept_names=list(entry.concept_names) if entry else [],
                    services=[service],
                )
            else:
                da = seen[dep_id]
                if service not in da.services:
                    seen[dep_id] = da.model_copy(update={"services": da.services + [service]})

    for pyproj in sorted(repos_root.rglob("pyproject.toml")):
        service = pyproj.parents[1].name
        for pkg, version in _parse_pyproject_toml(pyproj):
            dep_id = _artifact_id(pkg)
            entry = fc.lookup(pkg)
            if dep_id not in seen:
                seen[dep_id] = DependencyArtifact(
                    id=dep_id,
                    ecosystem="pip",
                    group="",
                    artifact=pkg,
                    version=version,
                    scope="compile",
                    features=list(entry.features) if entry else [],
                    concept_names=list(entry.concept_names) if entry else [],
                    services=[service],
                )
            else:
                da = seen[dep_id]
                if service not in da.services:
                    seen[dep_id] = da.model_copy(update={"services": da.services + [service]})

    return list(seen.values())
