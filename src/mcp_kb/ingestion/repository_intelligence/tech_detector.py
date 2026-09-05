"""Technology Detection Engine.

Scans repository files (build files, manifests, configs, Dockerfiles, etc.)
and emits ``TechEvidence`` signals without any hardcoded technology rules.

Design principles
-----------------
* Every detection strategy is self-contained and registered via a decorator.
* Adding support for a new ecosystem (e.g. Rust/Cargo) requires only adding a
  new ``@detector`` function — no changes to the engine or pipeline.
* Confidence scores are additive: multiple weak signals combine into a strong
  conclusion at the aggregation layer.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import yaml

from ...logging import get_logger
from .models import (
    BuildSystem,
    DependencyModel,
    DeploymentModel,
    DeploymentTarget,
    FrameworkModel,
    IntegrationModel,
    ObservabilityModel,
    PrimaryLanguage,
    SecurityModel,
    TechEvidence,
    TechStackModel,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Detector registry
# ---------------------------------------------------------------------------

DetectorFn = Callable[[Path, "DetectionContext"], list[TechEvidence]]
_DETECTORS: list[tuple[str, DetectorFn]] = []


def detector(name: str) -> Callable[[DetectorFn], DetectorFn]:
    """Register a detector function by name.

    Usage::

        @detector("pom_xml")
        def _detect_pom(repo_root: Path, ctx: DetectionContext) -> list[TechEvidence]:
            ...
    """
    def _wrap(fn: DetectorFn) -> DetectorFn:
        _DETECTORS.append((name, fn))
        return fn
    return _wrap


# ---------------------------------------------------------------------------
# Detection context (mutable accumulator passed to all detectors)
# ---------------------------------------------------------------------------

class DetectionContext:
    """Accumulates evidence across all detectors for a single repository."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.evidence: list[TechEvidence] = []
        self.tech_stack = TechStackModel()
        self.frameworks: list[FrameworkModel] = []
        self.dependencies: list[DependencyModel] = []
        self.deployment = DeploymentModel()
        self.integrations = IntegrationModel()
        self.observability = ObservabilityModel()
        self.security = SecurityModel()

    def add(self, evidence: TechEvidence) -> None:
        """Record one piece of detected technology evidence."""
        self.evidence.append(evidence)
        self.tech_stack.evidence.append(evidence)

    def add_many(self, items: list[TechEvidence]) -> None:
        """Record a batch of detected technology evidence."""
        for item in items:
            self.add(item)

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _find_files(root: Path, *patterns: str) -> list[Path]:
    results: list[Path] = []
    for pattern in patterns:
        results.extend(root.rglob(pattern))
    return results


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _xml_root(path: Path) -> ET.Element | None:
    try:
        return ET.parse(str(path)).getroot()
    except ET.ParseError:
        return None


def _load_yaml(path: Path) -> dict | list | None:
    try:
        return yaml.safe_load(_read_text(path))
    except yaml.YAMLError:
        return None


def _dependency_signal(artifact_id: str, group_id: str, version: str, source: str) -> str:
    return f"dependency:{group_id}:{artifact_id}:{version}" if version else \
           f"dependency:{group_id}:{artifact_id}"


# ---------------------------------------------------------------------------
# ── DETECTOR: Maven pom.xml ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------

_MAVEN_NS = "http://maven.apache.org/POM/4.0.0"


def _strip_ns(tag: str) -> str:
    return re.sub(r"\{[^}]+\}", "", tag)


def _pom_text(elem: ET.Element, tag: str, ns: str = _MAVEN_NS) -> str:
    child = elem.find(f"{{{ns}}}{tag}")
    if child is None:
        child = elem.find(tag)
    return (child.text or "").strip() if child is not None else ""


@detector("maven_pom")
def _detect_pom(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for pom_path in _find_files(root, "pom.xml"):
        rel = ctx._rel(pom_path)
        xml_root = _xml_root(pom_path)
        if xml_root is None:
            continue

        ctx.tech_stack.build_systems.append(BuildSystem.MAVEN)
        if ctx.tech_stack.primary_language == PrimaryLanguage.UNKNOWN:
            ctx.tech_stack.primary_language = PrimaryLanguage.JAVA

        evidence.append(TechEvidence(
            signal="build:maven:pom.xml",
            source_file=rel,
            confidence=1.0,
            technology="Maven",
        ))

        # Parent POM → Spring Boot BOM detection
        parent = xml_root.find(f"{{{_MAVEN_NS}}}parent") or xml_root.find("parent")
        if parent is not None:
            parent_artifact = _pom_text(parent, "artifactId")
            parent_group = _pom_text(parent, "groupId")
            parent_version = _pom_text(parent, "version")
            if parent_artifact:
                tech = _infer_tech_from_dependency(parent_group, parent_artifact)
                evidence.append(TechEvidence(
                    signal=f"parent:{parent_group}:{parent_artifact}:{parent_version}",
                    source_file=rel,
                    confidence=0.95,
                    technology=tech or parent_group,
                    version=parent_version or None,
                ))
                if tech:
                    _add_framework(ctx, tech, parent_version)

        # All <dependency> blocks
        deps = DependencyModel(ecosystem="maven")
        for dep in xml_root.iter(f"{{{_MAVEN_NS}}}dependency"):
            group = _pom_text(dep, "groupId")
            artifact = _pom_text(dep, "artifactId")
            version = _pom_text(dep, "version")
            scope = _pom_text(dep, "scope") or "compile"
            if not artifact:
                continue
            raw = _dependency_signal(artifact, group, version, rel)
            ctx.tech_stack.raw_dependencies.append(raw)
            (deps.test_scoped if scope == "test" else
             deps.provided if scope == "provided" else
             deps.direct).append(f"{group}:{artifact}:{version}".rstrip(":"))

            tech = _infer_tech_from_dependency(group, artifact)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"dependency:{group}:{artifact}",
                    source_file=rel,
                    confidence=0.9,
                    technology=tech,
                    version=version or None,
                ))
                _add_technology(ctx, tech, artifact, version)

        ctx.dependencies.append(deps)

        # <properties> — detect Java version
        props = xml_root.find(f"{{{_MAVEN_NS}}}properties") or xml_root.find("properties")
        if props is not None:
            for child in props:
                tag = _strip_ns(child.tag)
                val = (child.text or "").strip()
                if "java.version" in tag or "maven.compiler.source" in tag:
                    evidence.append(TechEvidence(
                        signal=f"java_version:{val}",
                        source_file=rel,
                        confidence=1.0,
                        technology="Java",
                        version=val,
                    ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Gradle build files ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("gradle")
def _detect_gradle(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "build.gradle", "build.gradle.kts"):
        rel = ctx._rel(f)
        text = _read_text(f)
        if not text:
            continue

        ctx.tech_stack.build_systems.append(BuildSystem.GRADLE)
        if ctx.tech_stack.primary_language == PrimaryLanguage.UNKNOWN:
            ctx.tech_stack.primary_language = (
                PrimaryLanguage.KOTLIN if f.suffix == ".kts" else PrimaryLanguage.JAVA
            )
        evidence.append(TechEvidence(
            signal=f"build:gradle:{f.name}",
            source_file=rel,
            confidence=1.0,
            technology="Gradle",
        ))

        # Extract dependency strings e.g. implementation("org.springframework.boot:...")
        for m in re.finditer(
            r"""['"]([\w.\-]+):([\w.\-]+)(?::([\w.\-]+))?['"]""", text
        ):
            group, artifact, version = m.group(1), m.group(2), m.group(3) or ""
            tech = _infer_tech_from_dependency(group, artifact)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"dependency:{group}:{artifact}",
                    source_file=rel,
                    confidence=0.85,
                    technology=tech,
                    version=version or None,
                ))
                _add_technology(ctx, tech, artifact, version)

        # plugins block
        for m in re.finditer(r"""id\s*[('"]+([\w.\-]+)['"]+""", text):
            plugin = m.group(1)
            tech = _infer_tech_from_plugin(plugin)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"gradle_plugin:{plugin}",
                    source_file=rel,
                    confidence=0.9,
                    technology=tech,
                ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: npm / package.json ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("npm_package_json")
def _detect_npm(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "package.json"):
        if "node_modules" in f.parts:
            continue
        rel = ctx._rel(f)
        try:
            import json
            data = json.loads(_read_text(f))
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("npm_package_json_skipped", path=str(f), error=str(exc))
            continue
        if not isinstance(data, dict):
            continue

        ctx.tech_stack.build_systems.append(BuildSystem.NPM)
        if ctx.tech_stack.primary_language == PrimaryLanguage.UNKNOWN:
            ctx.tech_stack.primary_language = PrimaryLanguage.JAVASCRIPT

        evidence.append(TechEvidence(
            signal="build:npm:package.json",
            source_file=rel,
            confidence=1.0,
            technology="npm",
        ))

        all_deps: dict[str, str] = {}
        all_deps.update(data.get("dependencies", {}))
        all_deps.update(data.get("devDependencies", {}))
        all_deps.update(data.get("peerDependencies", {}))

        for pkg, version in all_deps.items():
            tech = _infer_tech_from_npm_package(pkg)
            raw = f"{pkg}@{version}"
            ctx.tech_stack.raw_dependencies.append(raw)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"npm_dep:{pkg}",
                    source_file=rel,
                    confidence=0.9,
                    technology=tech,
                    version=version,
                ))
                _add_technology(ctx, tech, pkg, version)

        # TypeScript detection
        if "typescript" in all_deps or (f.parent / "tsconfig.json").exists():
            ctx.tech_stack.primary_language = PrimaryLanguage.TYPESCRIPT
            evidence.append(TechEvidence(
                signal="typescript:tsconfig_or_dep",
                source_file=rel,
                confidence=0.95,
                technology="TypeScript",
            ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Python requirements / setup.py / pyproject.toml ───────────────
# ---------------------------------------------------------------------------

@detector("python")
def _detect_python(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    req_files = _find_files(root, "requirements*.txt", "requirements/*.txt")
    pyprojects = _find_files(root, "pyproject.toml")
    setup_pys = _find_files(root, "setup.py", "setup.cfg")

    if not (req_files or pyprojects or setup_pys):
        return evidence

    ctx.tech_stack.primary_language = PrimaryLanguage.PYTHON
    ctx.tech_stack.build_systems.append(BuildSystem.PIP)

    for f in req_files:
        rel = ctx._rel(f)
        for line in _read_text(f).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[>=<!~\[]", line)[0].strip().lower()
            version = line[len(pkg):].strip() if len(line) > len(pkg) else ""
            ctx.tech_stack.raw_dependencies.append(line)
            tech = _infer_tech_from_pip_package(pkg)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"pip_dep:{pkg}",
                    source_file=rel,
                    confidence=0.9,
                    technology=tech,
                    version=version or None,
                ))
                _add_technology(ctx, tech, pkg, version)

    for f in pyprojects:
        rel = ctx._rel(f)
        text = _read_text(f)
        # Poetry uses pyproject.toml
        if "[tool.poetry]" in text:
            ctx.tech_stack.build_systems.append(BuildSystem.POETRY)
            evidence.append(TechEvidence(
                signal="build:poetry:pyproject.toml",
                source_file=rel,
                confidence=1.0,
                technology="Poetry",
            ))
        # FastAPI, Django, Flask, etc. in [project] dependencies
        for m in re.finditer(r"""["']([\w\-]+)(?:\s*[>=<!~]+\s*[\w.*]+)?["']""", text):
            pkg = m.group(1).lower()
            tech = _infer_tech_from_pip_package(pkg)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"pyproject_dep:{pkg}",
                    source_file=rel,
                    confidence=0.85,
                    technology=tech,
                ))
                _add_technology(ctx, tech, pkg, "")

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: .NET / .csproj / .sln ─────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("dotnet")
def _detect_dotnet(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "*.csproj", "*.fsproj", "*.vbproj"):
        rel = ctx._rel(f)
        ctx.tech_stack.primary_language = PrimaryLanguage.CSHARP
        ctx.tech_stack.build_systems.append(BuildSystem.DOTNET)
        xml_root = _xml_root(f)
        if xml_root is None:
            evidence.append(TechEvidence(
                signal="build:dotnet:csproj",
                source_file=rel,
                confidence=1.0,
                technology=".NET",
            ))
            continue

        evidence.append(TechEvidence(
            signal="build:dotnet:csproj",
            source_file=rel,
            confidence=1.0,
            technology=".NET",
        ))

        # Target framework
        for elem in xml_root.iter("TargetFramework"):
            tf = (elem.text or "").strip()
            if tf:
                evidence.append(TechEvidence(
                    signal=f"dotnet_target:{tf}",
                    source_file=rel,
                    confidence=1.0,
                    technology=".NET",
                    version=tf,
                ))

        # NuGet package references
        for pkg_ref in xml_root.iter("PackageReference"):
            name = pkg_ref.get("Include", "")
            version = pkg_ref.get("Version", "")
            if not name:
                continue
            ctx.tech_stack.raw_dependencies.append(f"{name}:{version}")
            tech = _infer_tech_from_nuget(name)
            if tech:
                evidence.append(TechEvidence(
                    signal=f"nuget:{name}",
                    source_file=rel,
                    confidence=0.9,
                    technology=tech,
                    version=version or None,
                ))
                _add_technology(ctx, tech, name, version)

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Go modules ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("go_modules")
def _detect_go(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "go.mod"):
        rel = ctx._rel(f)
        ctx.tech_stack.primary_language = PrimaryLanguage.GO
        ctx.tech_stack.build_systems.append(BuildSystem.GO_MOD)
        evidence.append(TechEvidence(
            signal="build:go:go.mod",
            source_file=rel,
            confidence=1.0,
            technology="Go",
        ))
        for line in _read_text(f).splitlines():
            m = re.match(r"\s*require\s+([\w./\-]+)\s+([\w.+\-/]+)", line)
            if m:
                pkg, version = m.group(1), m.group(2)
                ctx.tech_stack.raw_dependencies.append(f"{pkg}@{version}")
                tech = _infer_tech_from_go_module(pkg)
                if tech:
                    evidence.append(TechEvidence(
                        signal=f"go_dep:{pkg}",
                        source_file=rel,
                        confidence=0.85,
                        technology=tech,
                        version=version,
                    ))
                    _add_technology(ctx, tech, pkg, version)

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Dockerfile / docker-compose ───────────────────────────────────
# ---------------------------------------------------------------------------

@detector("docker")
def _detect_docker(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "Dockerfile", "Dockerfile.*", "*.dockerfile"):
        rel = ctx._rel(f)
        ctx.deployment.has_dockerfile = True
        if DeploymentTarget.DOCKER not in ctx.deployment.targets:
            ctx.deployment.targets.append(DeploymentTarget.DOCKER)
        evidence.append(TechEvidence(
            signal="deploy:docker:Dockerfile",
            source_file=rel,
            confidence=1.0,
            technology="Docker",
        ))
        for line in _read_text(f).splitlines():
            m = re.match(r"FROM\s+([\w.\-/:@]+)", line.strip(), re.IGNORECASE)
            if m:
                image = m.group(1)
                ctx.deployment.container_base_images.append(image)
                evidence.append(TechEvidence(
                    signal=f"docker_base_image:{image}",
                    source_file=rel,
                    confidence=0.9,
                    technology="Docker",
                    metadata={"base_image": image},
                ))
            # ENV declarations
            m2 = re.match(r"ENV\s+([\w_]+)", line.strip(), re.IGNORECASE)
            if m2:
                ctx.deployment.environment_variables.append(m2.group(1))
            # EXPOSE
            m3 = re.match(r"EXPOSE\s+(\d+)", line.strip(), re.IGNORECASE)
            if m3:
                ctx.deployment.exposed_ports.append(int(m3.group(1)))

    for f in _find_files(root, "docker-compose*.yml", "docker-compose*.yaml"):
        rel = ctx._rel(f)
        ctx.deployment.has_docker_compose = True
        evidence.append(TechEvidence(
            signal="deploy:docker:docker-compose",
            source_file=rel,
            confidence=1.0,
            technology="Docker Compose",
        ))
        data = _load_yaml(f)
        if isinstance(data, dict):
            for svc_name, svc in (data.get("services") or {}).items():
                if isinstance(svc, dict):
                    for dep_svc in (svc.get("depends_on") or []):
                        ctx.integrations.external_services.append(str(dep_svc))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Kubernetes manifests ──────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("kubernetes")
def _detect_kubernetes(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    k8s_kinds = {"Deployment", "Service", "Ingress", "ConfigMap", "Secret",
                 "StatefulSet", "DaemonSet", "CronJob", "Job", "HorizontalPodAutoscaler"}

    for f in _find_files(root, "*.yaml", "*.yml"):
        if any(skip in f.parts for skip in ("node_modules", ".git", "vendor")):
            continue
        rel = ctx._rel(f)
        data = _load_yaml(f)
        if not isinstance(data, dict):
            continue
        kind = data.get("kind", "")
        api_version = data.get("apiVersion", "")
        if kind in k8s_kinds or ("kubernetes.io" in api_version):
            if not ctx.deployment.has_kubernetes_manifests:
                ctx.deployment.has_kubernetes_manifests = True
                if DeploymentTarget.KUBERNETES not in ctx.deployment.targets:
                    ctx.deployment.targets.append(DeploymentTarget.KUBERNETES)
            evidence.append(TechEvidence(
                signal=f"k8s:{kind}",
                source_file=rel,
                confidence=0.95,
                technology="Kubernetes",
                metadata={"kind": kind, "apiVersion": api_version},
            ))
            meta = data.get("metadata", {}) or {}
            ns = meta.get("namespace", "")
            if ns and ns not in ctx.deployment.k8s_namespaces:
                ctx.deployment.k8s_namespaces.append(ns)

    # Helm chart detection
    for f in _find_files(root, "Chart.yaml"):
        rel = ctx._rel(f)
        ctx.deployment.has_helm_chart = True
        if DeploymentTarget.HELM not in ctx.deployment.targets:
            ctx.deployment.targets.append(DeploymentTarget.HELM)
        data = _load_yaml(f)
        if isinstance(data, dict):
            ctx.deployment.helm_chart_name = data.get("name")
        evidence.append(TechEvidence(
            signal="deploy:helm:Chart.yaml",
            source_file=rel,
            confidence=1.0,
            technology="Helm",
        ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Terraform ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("terraform")
def _detect_terraform(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "*.tf", "*.tfvars"):
        rel = ctx._rel(f)
        if not ctx.deployment.has_terraform:
            ctx.deployment.has_terraform = True
            if DeploymentTarget.TERRAFORM not in ctx.deployment.targets:
                ctx.deployment.targets.append(DeploymentTarget.TERRAFORM)
        evidence.append(TechEvidence(
            signal=f"deploy:terraform:{f.name}",
            source_file=rel,
            confidence=1.0,
            technology="Terraform",
        ))
        text = _read_text(f)
        for m in re.finditer(r'provider\s+"([\w\-]+)"', text):
            provider = m.group(1)
            if provider not in ctx.deployment.terraform_providers:
                ctx.deployment.terraform_providers.append(provider)
            evidence.append(TechEvidence(
                signal=f"terraform_provider:{provider}",
                source_file=rel,
                confidence=0.9,
                technology=f"Terraform/{provider}",
            ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: OpenAPI / Swagger specs ───────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("openapi")
def _detect_openapi(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "openapi*.yaml", "openapi*.yml", "openapi*.json",
                         "swagger*.yaml", "swagger*.yml", "swagger*.json"):
        rel = ctx._rel(f)
        ctx.integrations.openapi_specs.append(rel)
        ctx.tech_stack.doc_sources.append(rel)
        evidence.append(TechEvidence(
            signal=f"openapi_spec:{f.name}",
            source_file=rel,
            confidence=0.95,
            technology="OpenAPI",
        ))

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Spring application*.yml / bootstrap*.yml ──────────────────────
# ---------------------------------------------------------------------------

@detector("spring_config")
def _detect_spring_config(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "application*.yml", "application*.yaml",
                         "application*.properties", "bootstrap*.yml",
                         "bootstrap*.yaml", "bootstrap*.properties"):
        rel = ctx._rel(f)
        text = _read_text(f)

        evidence.append(TechEvidence(
            signal=f"spring_config:{f.name}",
            source_file=rel,
            confidence=0.9,
            technology="Spring Boot",
        ))

        # Spring application name
        m = re.search(r"spring\.application\.name\s*[=:]\s*([\w\-]+)", text)
        if m:
            evidence.append(TechEvidence(
                signal=f"spring_app_name:{m.group(1)}",
                source_file=rel,
                confidence=1.0,
                technology="Spring Boot",
                metadata={"app_name": m.group(1)},
            ))

        _detect_spring_tech_from_config(text, rel, ctx, evidence)

    return evidence


def _detect_spring_tech_from_config(
    text: str, source_file: str, ctx: DetectionContext, evidence: list[TechEvidence]
) -> None:
    patterns = [
        (r"spring\.kafka", "Apache Kafka"),
        (r"spring\.datasource", "Spring Data"),
        (r"spring\.jpa", "Spring Data JPA"),
        (r"spring\.security", "Spring Security"),
        (r"spring\.cloud\.gateway", "Spring Cloud Gateway"),
        (r"spring\.cloud\.config", "Spring Cloud Config"),
        (r"eureka\.client", "Netflix Eureka"),
        (r"spring\.redis", "Redis"),
        (r"spring\.data\.mongodb", "MongoDB"),
        (r"spring\.elasticsearch", "Elasticsearch"),
        (r"management\.endpoints", "Spring Actuator"),
        (r"resilience4j\.", "Resilience4j"),
        (r"springdoc\.", "SpringDoc OpenAPI"),
        (r"keycloak\.", "Keycloak"),
        (r"spring\.vault", "HashiCorp Vault"),
    ]
    for pattern, tech in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            evidence.append(TechEvidence(
                signal=f"spring_config_key:{pattern}",
                source_file=source_file,
                confidence=0.85,
                technology=tech,
            ))
            _add_technology(ctx, tech, "", "")


# ---------------------------------------------------------------------------
# ── DETECTOR: Java source file imports / annotations ────────────────────────
# ---------------------------------------------------------------------------

@detector("java_source")
def _detect_java_source(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    """Scan a representative sample of .java files for import-based tech signals."""
    evidence: list[TechEvidence] = []
    java_files = list(root.rglob("*.java"))
    if not java_files:
        return evidence

    ctx.tech_stack.primary_language = PrimaryLanguage.JAVA

    # Sample up to 100 files to avoid scanning large repos exhaustively
    sample = java_files[:100]
    tech_imports: dict[str, set[str]] = {}

    for f in sample:
        rel = ctx._rel(f)
        text = _read_text(f)
        for imp in re.findall(r"^import\s+([\w.]+);", text, re.MULTILINE):
            tech = _infer_tech_from_java_import(imp)
            if tech:
                tech_imports.setdefault(tech, set()).add(rel)

    for tech, files in tech_imports.items():
        evidence.append(TechEvidence(
            signal=f"java_import:{tech}",
            source_file=next(iter(files)),
            confidence=0.8,
            technology=tech,
            metadata={"found_in_files": len(files)},
        ))
        _add_technology(ctx, tech, "", "")

    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: TypeScript / tsconfig ─────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("typescript")
def _detect_typescript(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    for f in _find_files(root, "tsconfig*.json"):
        rel = ctx._rel(f)
        ctx.tech_stack.primary_language = PrimaryLanguage.TYPESCRIPT
        evidence.append(TechEvidence(
            signal="typescript:tsconfig.json",
            source_file=rel,
            confidence=1.0,
            technology="TypeScript",
        ))
    return evidence


# ---------------------------------------------------------------------------
# ── DETECTOR: Makefile / Bazel ───────────────────────────────────────────────
# ---------------------------------------------------------------------------

@detector("makefile_bazel")
def _detect_make_bazel(root: Path, ctx: DetectionContext) -> list[TechEvidence]:
    evidence: list[TechEvidence] = []
    if (root / "Makefile").exists():
        ctx.tech_stack.build_systems.append(BuildSystem.MAKEFILE)
        evidence.append(TechEvidence(
            signal="build:makefile",
            source_file="Makefile",
            confidence=0.8,
            technology="Make",
        ))
    for f in _find_files(root, "BUILD", "BUILD.bazel", "WORKSPACE"):
        ctx.tech_stack.build_systems.append(BuildSystem.BAZEL)
        evidence.append(TechEvidence(
            signal="build:bazel",
            source_file=ctx._rel(f),
            confidence=1.0,
            technology="Bazel",
        ))
        break
    return evidence


# ---------------------------------------------------------------------------
# Technology inference tables (no hardcoded enumerations — pure pattern matching)
# ---------------------------------------------------------------------------

_MAVEN_TECH_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"spring-boot", re.IGNORECASE),         "Spring Boot"),
    (re.compile(r"spring-security", re.IGNORECASE),     "Spring Security"),
    (re.compile(r"spring-data-jpa", re.IGNORECASE),     "Spring Data JPA"),
    (re.compile(r"spring-data", re.IGNORECASE),         "Spring Data"),
    (re.compile(r"spring-kafka", re.IGNORECASE),        "Apache Kafka"),
    (re.compile(r"spring-cloud", re.IGNORECASE),        "Spring Cloud"),
    (re.compile(r"spring-web", re.IGNORECASE),          "Spring MVC"),
    (re.compile(r"hibernate", re.IGNORECASE),           "Hibernate"),
    (re.compile(r"liquibase", re.IGNORECASE),           "Liquibase"),
    (re.compile(r"flyway", re.IGNORECASE),              "Flyway"),
    (re.compile(r"kafka", re.IGNORECASE),               "Apache Kafka"),
    (re.compile(r"resilience4j", re.IGNORECASE),        "Resilience4j"),
    (re.compile(r"feign", re.IGNORECASE),               "OpenFeign"),
    (re.compile(r"micrometer", re.IGNORECASE),          "Micrometer"),
    (re.compile(r"opentelemetry", re.IGNORECASE),       "OpenTelemetry"),
    (re.compile(r"zipkin", re.IGNORECASE),              "Zipkin"),
    (re.compile(r"keycloak", re.IGNORECASE),            "Keycloak"),
    (re.compile(r"mapstruct", re.IGNORECASE),           "MapStruct"),
    (re.compile(r"lombok", re.IGNORECASE),              "Lombok"),
    (re.compile(r"testcontainers", re.IGNORECASE),      "Testcontainers"),
    (re.compile(r"junit", re.IGNORECASE),               "JUnit"),
    (re.compile(r"mockito", re.IGNORECASE),             "Mockito"),
    (re.compile(r"h2", re.IGNORECASE),                  "H2 Database"),
    (re.compile(r"postgresql", re.IGNORECASE),          "PostgreSQL"),
    (re.compile(r"mysql", re.IGNORECASE),               "MySQL"),
    (re.compile(r"mongodb", re.IGNORECASE),             "MongoDB"),
    (re.compile(r"redis", re.IGNORECASE),               "Redis"),
    (re.compile(r"elasticsearch", re.IGNORECASE),       "Elasticsearch"),
    (re.compile(r"actuator", re.IGNORECASE),            "Spring Actuator"),
    (re.compile(r"springdoc", re.IGNORECASE),           "SpringDoc OpenAPI"),
    (re.compile(r"swagger", re.IGNORECASE),             "Swagger/OpenAPI"),
    (re.compile(r"aws", re.IGNORECASE),                 "AWS"),
    (re.compile(r"azure", re.IGNORECASE),               "Azure"),
    (re.compile(r"gcp|google-cloud", re.IGNORECASE),    "Google Cloud"),
]

_NPM_TECH_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^@angular/", re.IGNORECASE),          "Angular"),
    (re.compile(r"^react$", re.IGNORECASE),             "React"),
    (re.compile(r"^react-dom$", re.IGNORECASE),         "React"),
    (re.compile(r"^vue$", re.IGNORECASE),               "Vue.js"),
    (re.compile(r"^next$", re.IGNORECASE),              "Next.js"),
    (re.compile(r"^nuxt$", re.IGNORECASE),              "Nuxt.js"),
    (re.compile(r"^express$", re.IGNORECASE),           "Express.js"),
    (re.compile(r"^nestjs|^@nestjs/", re.IGNORECASE),   "NestJS"),
    (re.compile(r"^kafka", re.IGNORECASE),              "Apache Kafka"),
    (re.compile(r"^rxjs$", re.IGNORECASE),              "RxJS"),
    (re.compile(r"^axios$", re.IGNORECASE),             "Axios"),
    (re.compile(r"^jest$", re.IGNORECASE),              "Jest"),
    (re.compile(r"^cypress$", re.IGNORECASE),           "Cypress"),
    (re.compile(r"^graphql$", re.IGNORECASE),           "GraphQL"),
    (re.compile(r"^typeorm$", re.IGNORECASE),           "TypeORM"),
    (re.compile(r"^prisma$", re.IGNORECASE),            "Prisma"),
    (re.compile(r"^redis$", re.IGNORECASE),             "Redis"),
    (re.compile(r"^mongodb$", re.IGNORECASE),           "MongoDB"),
    (re.compile(r"^tailwindcss$", re.IGNORECASE),       "Tailwind CSS"),
    (re.compile(r"^webpack$", re.IGNORECASE),           "Webpack"),
    (re.compile(r"^vite$", re.IGNORECASE),              "Vite"),
]

_PIP_TECH_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^django$", re.IGNORECASE),            "Django"),
    (re.compile(r"^flask$", re.IGNORECASE),             "Flask"),
    (re.compile(r"^fastapi$", re.IGNORECASE),           "FastAPI"),
    (re.compile(r"^uvicorn$", re.IGNORECASE),           "Uvicorn/ASGI"),
    (re.compile(r"^sqlalchemy$", re.IGNORECASE),        "SQLAlchemy"),
    (re.compile(r"^celery$", re.IGNORECASE),            "Celery"),
    (re.compile(r"^kafka", re.IGNORECASE),              "Apache Kafka"),
    (re.compile(r"^pydantic$", re.IGNORECASE),          "Pydantic"),
    (re.compile(r"^pytest$", re.IGNORECASE),            "pytest"),
    (re.compile(r"^redis$", re.IGNORECASE),             "Redis"),
    (re.compile(r"^boto3$", re.IGNORECASE),             "AWS"),
    (re.compile(r"^google-cloud", re.IGNORECASE),       "Google Cloud"),
    (re.compile(r"^azure", re.IGNORECASE),              "Azure"),
    (re.compile(r"^langchain", re.IGNORECASE),          "LangChain"),
    (re.compile(r"^openai$", re.IGNORECASE),            "OpenAI"),
    (re.compile(r"^neo4j$", re.IGNORECASE),             "Neo4j"),
    (re.compile(r"^qdrant", re.IGNORECASE),             "Qdrant"),
]

_NUGET_TECH_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^Microsoft\.AspNetCore", re.IGNORECASE),   "ASP.NET Core"),
    (re.compile(r"^Microsoft\.EntityFramework", re.IGNORECASE), "Entity Framework"),
    (re.compile(r"^Microsoft\.Extensions", re.IGNORECASE),   ".NET Extensions"),
    (re.compile(r"^Confluent\.Kafka", re.IGNORECASE),        "Apache Kafka"),
    (re.compile(r"^Serilog", re.IGNORECASE),                 "Serilog"),
    (re.compile(r"^NUnit|^xunit|^MSTest", re.IGNORECASE),    "Test Framework (.NET)"),
    (re.compile(r"^Moq$", re.IGNORECASE),                    "Moq"),
    (re.compile(r"^AutoMapper$", re.IGNORECASE),             "AutoMapper"),
    (re.compile(r"^MediatR$", re.IGNORECASE),                "MediatR"),
    (re.compile(r"^FluentValidation", re.IGNORECASE),        "FluentValidation"),
    (re.compile(r"^StackExchange\.Redis", re.IGNORECASE),    "Redis"),
    (re.compile(r"^Npgsql", re.IGNORECASE),                  "PostgreSQL"),
    (re.compile(r"^MongoDB", re.IGNORECASE),                 "MongoDB"),
    (re.compile(r"^AWSSDK", re.IGNORECASE),                  "AWS"),
    (re.compile(r"^Azure\.", re.IGNORECASE),                 "Azure"),
    (re.compile(r"^Swashbuckle", re.IGNORECASE),             "Swagger/OpenAPI"),
    (re.compile(r"^Polly$", re.IGNORECASE),                  "Polly"),
]

_JAVA_IMPORT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^org\.springframework\.web"),      "Spring MVC"),
    (re.compile(r"^org\.springframework\.security"), "Spring Security"),
    (re.compile(r"^org\.springframework\.data"),     "Spring Data"),
    (re.compile(r"^org\.springframework\.kafka"),    "Apache Kafka"),
    (re.compile(r"^org\.springframework\.cloud"),    "Spring Cloud"),
    (re.compile(r"^org\.springframework"),           "Spring Framework"),
    (re.compile(r"^jakarta\.persistence"),           "Jakarta Persistence/JPA"),
    (re.compile(r"^javax\.persistence"),             "JPA"),
    (re.compile(r"^org\.hibernate"),                 "Hibernate"),
    (re.compile(r"^org\.apache\.kafka"),             "Apache Kafka"),
    (re.compile(r"^io\.micrometer"),                 "Micrometer"),
    (re.compile(r"^io\.opentelemetry"),              "OpenTelemetry"),
    (re.compile(r"^io\.github\.resilience4j"),       "Resilience4j"),
    (re.compile(r"^feign\.|^org\.springframework\.cloud\.openfeign"), "OpenFeign"),
    (re.compile(r"^org\.keycloak"),                  "Keycloak"),
    (re.compile(r"^org\.mapstruct"),                 "MapStruct"),
    (re.compile(r"^lombok\.|^org\.projectlombok"),   "Lombok"),
    (re.compile(r"^org\.junit"),                     "JUnit"),
    (re.compile(r"^org\.mockito"),                   "Mockito"),
    (re.compile(r"^software\.amazon"),               "AWS"),
    (re.compile(r"^com\.azure"),                     "Azure"),
    (re.compile(r"^com\.google\.cloud"),             "Google Cloud"),
    (re.compile(r"^liquibase\."),                    "Liquibase"),
    (re.compile(r"^org\.flywaydb"),                  "Flyway"),
    (re.compile(r"^io\.swagger|^springfox"),         "Swagger/OpenAPI"),
    (re.compile(r"^org\.springdoc"),                 "SpringDoc OpenAPI"),
]

_GO_MODULE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"gin-gonic/gin"),          "Gin"),
    (re.compile(r"labstack/echo"),          "Echo"),
    (re.compile(r"gofiber/fiber"),          "Fiber"),
    (re.compile(r"gorilla/mux"),            "Gorilla Mux"),
    (re.compile(r"grpc"),                   "gRPC"),
    (re.compile(r"kafka"),                  "Apache Kafka"),
    (re.compile(r"gorm"),                   "GORM"),
    (re.compile(r"go-redis"),               "Redis"),
    (re.compile(r"mongo-driver"),           "MongoDB"),
    (re.compile(r"aws-sdk-go"),             "AWS"),
    (re.compile(r"google-cloud-go"),        "Google Cloud"),
    (re.compile(r"opentelemetry"),          "OpenTelemetry"),
]

_GRADLE_PLUGIN_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"org\.springframework\.boot"), "Spring Boot"),
    (re.compile(r"io\.spring\.dependency"),     "Spring Dependency Management"),
    (re.compile(r"kotlin"),                     "Kotlin"),
    (re.compile(r"com\.google\.protobuf"),      "Protocol Buffers"),
    (re.compile(r"openapi"),                    "OpenAPI"),
    (re.compile(r"jacoco"),                     "JaCoCo"),
    (re.compile(r"sonarqube"),                  "SonarQube"),
]


def _infer_tech_from_dependency(group: str, artifact: str) -> str | None:
    combined = f"{group}:{artifact}"
    for pattern, tech in _MAVEN_TECH_MAP:
        if pattern.search(combined):
            return tech
    return None


def _infer_tech_from_npm_package(pkg: str) -> str | None:
    for pattern, tech in _NPM_TECH_MAP:
        if pattern.search(pkg):
            return tech
    return None


def _infer_tech_from_pip_package(pkg: str) -> str | None:
    for pattern, tech in _PIP_TECH_MAP:
        if pattern.search(pkg):
            return tech
    return None


def _infer_tech_from_nuget(name: str) -> str | None:
    for pattern, tech in _NUGET_TECH_MAP:
        if pattern.search(name):
            return tech
    return None


def _infer_tech_from_java_import(imp: str) -> str | None:
    for pattern, tech in _JAVA_IMPORT_MAP:
        if pattern.search(imp):
            return tech
    return None


def _infer_tech_from_go_module(pkg: str) -> str | None:
    for pattern, tech in _GO_MODULE_MAP:
        if pattern.search(pkg):
            return tech
    return None


def _infer_tech_from_plugin(plugin: str) -> str | None:
    for pattern, tech in _GRADLE_PLUGIN_MAP:
        if pattern.search(plugin):
            return tech
    return None


# ---------------------------------------------------------------------------
# Context mutation helpers
# ---------------------------------------------------------------------------

def _add_technology(ctx: DetectionContext, tech: str, artifact: str, version: str) -> None:
    """Route a detected technology to the correct sub-list on TechStackModel."""
    t = tech.lower()
    _messaging = {"apache kafka", "rabbitmq", "activemq", "pulsar", "nats", "aws sqs"}
    _persistence = {"hibernate", "spring data jpa", "spring data", "jpa",
                    "jakarta persistence/jpa", "postgresql", "mysql", "mongodb",
                    "redis", "h2 database", "elasticsearch", "flyway", "liquibase",
                    "sqlalchemy", "typeorm", "prisma", "gorm", "entity framework"}
    _security = {"spring security", "keycloak", "jwt", "oauth2", "hashicorp vault",
                 "azure ad", "auth0", "spring security oauth2"}
    _test = {"junit", "mockito", "testcontainers", "jest", "cypress", "pytest",
             "test framework (.net)", "moq"}
    _observability = {"micrometer", "opentelemetry", "zipkin", "jaeger", "prometheus",
                      "spring actuator", "serilog", "datadog"}
    _cloud = {"aws", "azure", "google cloud", "aws sqs", "azure service bus"}

    if t in _messaging and tech not in ctx.tech_stack.messaging_technologies:
        ctx.tech_stack.messaging_technologies.append(tech)
    elif t in _persistence and tech not in ctx.tech_stack.persistence_technologies:
        ctx.tech_stack.persistence_technologies.append(tech)
    elif t in _security and tech not in ctx.tech_stack.security_technologies:
        ctx.tech_stack.security_technologies.append(tech)
    elif t in _test and tech not in ctx.tech_stack.test_frameworks:
        ctx.tech_stack.test_frameworks.append(tech)
    elif t in _observability and tech not in ctx.tech_stack.observability_components:
        ctx.tech_stack.observability_components.append(tech)
    elif t in _cloud and tech not in ctx.tech_stack.cloud_platforms:
        ctx.tech_stack.cloud_platforms.append(tech)
    else:
        if tech not in ctx.tech_stack.frameworks and tech not in ctx.tech_stack.libraries:
            # Heuristic: a technology with "framework" / "boot" / "mvc" in the name
            # is a framework; otherwise a library.
            fw_signals = {"framework", "boot", "mvc", "cloud", "data", "security",
                          "django", "flask", "fastapi", "angular", "react", "vue",
                          "nestjs", "asp.net", ".net extensions"}
            if any(sig in t for sig in fw_signals):
                ctx.tech_stack.frameworks.append(tech)
            else:
                ctx.tech_stack.libraries.append(tech)

    # Observability side-effects
    _obs = ctx.observability
    if "actuator" in t:
        _obs.has_actuator = True
    if "micrometer" in t:
        _obs.has_micrometer = True
    if "opentelemetry" in t:
        _obs.has_opentelemetry = True
    if "zipkin" in t:
        _obs.has_zipkin = True
    if "prometheus" in t:
        _obs.has_prometheus = True

    # Security side-effects
    _sec = ctx.security
    if "spring security" in t:
        _sec.has_spring_security = True
    if "oauth2" in t or "oauth" in t:
        _sec.has_oauth2 = True
    if "jwt" in t:
        _sec.has_jwt = True
    if "keycloak" in t:
        _sec.has_keycloak = True
    if "vault" in t:
        _sec.has_vault = True


def _add_framework(ctx: DetectionContext, tech: str, version: str) -> None:
    """Add a detected framework to the frameworks model list."""
    if not any(f.name == tech for f in ctx.frameworks):
        ctx.frameworks.append(FrameworkModel(name=tech, version=version or None))
    elif version:
        for f in ctx.frameworks:
            if f.name == tech and not f.version:
                f.version = version


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class TechnologyDetector:
    """Runs all registered detectors against a repository root.

    Extending to a new technology:
    1. Write a function decorated with ``@detector("name")``.
    2. Optionally add entries to the relevant ``*_TECH_MAP`` table.
    3. No other changes are required.
    """

    def detect(self, repo_root: Path) -> DetectionContext:
        """Run all registered technology detectors against one repo and return the aggregated context."""
        ctx = DetectionContext(repo_root)
        for name, fn in _DETECTORS:
            try:
                new_evidence = fn(repo_root, ctx)
                ctx.add_many(new_evidence)
            except Exception as exc:
                log.warning("detector_failed", detector=name, error=str(exc))

        # Deduplicate lists
        ctx.tech_stack.build_systems = list(dict.fromkeys(ctx.tech_stack.build_systems))
        ctx.tech_stack.frameworks = list(dict.fromkeys(ctx.tech_stack.frameworks))
        ctx.tech_stack.libraries = list(dict.fromkeys(ctx.tech_stack.libraries))
        ctx.tech_stack.messaging_technologies = list(dict.fromkeys(ctx.tech_stack.messaging_technologies))
        ctx.tech_stack.persistence_technologies = list(dict.fromkeys(ctx.tech_stack.persistence_technologies))
        ctx.tech_stack.security_technologies = list(dict.fromkeys(ctx.tech_stack.security_technologies))
        ctx.tech_stack.test_frameworks = list(dict.fromkeys(ctx.tech_stack.test_frameworks))
        ctx.tech_stack.cloud_platforms = list(dict.fromkeys(ctx.tech_stack.cloud_platforms))
        ctx.tech_stack.observability_components = list(dict.fromkeys(ctx.tech_stack.observability_components))

        log.info(
            "tech_detection_complete",
            repo=str(repo_root),
            language=ctx.tech_stack.primary_language.value,
            frameworks=ctx.tech_stack.frameworks,
            evidence_count=len(ctx.evidence),
        )
        return ctx
