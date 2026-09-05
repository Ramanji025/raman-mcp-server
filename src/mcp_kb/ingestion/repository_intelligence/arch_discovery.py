"""Architecture Discovery Engine.

Infers the architectural style of a repository from structural evidence.
No hardcoded rules — every style is scored from weighted signals.

A new architecture style can be added by registering a ``ArchSignal`` set
under the new ``ArchitectureStyle`` enum value.  The scorer picks it up
automatically.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...logging import get_logger
from .models import ArchEvidence, ArchitectureModel, ArchitectureStyle

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Signal definition
# ---------------------------------------------------------------------------

@dataclass
class ArchSignal:
    """A single architecture evidence signal with a weight."""
    description: str
    style: ArchitectureStyle
    weight: float = 1.0


# ---------------------------------------------------------------------------
# Signal registry
# ---------------------------------------------------------------------------

ArchSignalFn = Callable[[Path], list[ArchSignal]]
_SIGNAL_PROVIDERS: list[tuple[str, ArchSignalFn]] = []


def arch_signal_provider(name: str) -> Callable[[ArchSignalFn], ArchSignalFn]:
    """Register an architecture signal provider."""
    def _wrap(fn: ArchSignalFn) -> ArchSignalFn:
        _SIGNAL_PROVIDERS.append((name, fn))
        return fn
    return _wrap


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _collect_packages(root: Path) -> list[str]:
    """Return all unique package/namespace names found under root."""
    packages: set[str] = set()
    for f in root.rglob("*.java"):
        m = re.search(r"^package\s+([\w.]+);", _read(f), re.MULTILINE)
        if m:
            packages.add(m.group(1))
    for f in root.rglob("*.cs"):
        m = re.search(r"^namespace\s+([\w.]+)", _read(f), re.MULTILINE)
        if m:
            packages.add(m.group(1))
    for f in root.rglob("*.py"):
        packages.add(f.parent.name)
    return list(packages)


def _dir_names(root: Path, max_depth: int = 4) -> set[str]:
    """Collect all directory names up to max_depth."""
    names: set[str] = set()

    def _walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for child in path.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                names.add(child.name.lower())
                _walk(child, depth + 1)

    try:
        _walk(root, 0)
    except PermissionError:
        pass
    return names


# ---------------------------------------------------------------------------
# ── SIGNAL PROVIDER: Package/directory structure ────────────────────────────
# ---------------------------------------------------------------------------

@arch_signal_provider("package_structure")
def _package_structure_signals(root: Path) -> list[ArchSignal]:
    signals: list[ArchSignal] = []
    dirs = _dir_names(root)
    packages = _collect_packages(root)
    all_names = dirs | {p.split(".")[-1].lower() for p in packages}

    # ── Layered Architecture ──────────────────────────────────────────────
    layered_layers = {"controller", "controllers", "service", "services",
                      "repository", "repositories", "entity", "entities",
                      "model", "models", "dto", "dtos", "mapper", "mappers"}
    layered_hits = all_names & layered_layers
    if len(layered_hits) >= 3:
        signals.append(ArchSignal(
            description=f"layered_packages:{sorted(layered_hits)}",
            style=ArchitectureStyle.LAYERED,
            weight=min(1.0, len(layered_hits) * 0.2),
        ))

    # ── Hexagonal / Ports & Adapters ─────────────────────────────────────
    hex_signals = {"port", "ports", "adapter", "adapters", "inbound",
                   "outbound", "driving", "driven", "primary", "secondary"}
    hex_hits = all_names & hex_signals
    if hex_hits:
        signals.append(ArchSignal(
            description=f"hexagonal_packages:{sorted(hex_hits)}",
            style=ArchitectureStyle.HEXAGONAL,
            weight=min(1.0, len(hex_hits) * 0.3),
        ))

    # ── Clean Architecture ────────────────────────────────────────────────
    clean_signals = {"usecase", "usecases", "use_case", "use_cases",
                     "interactor", "interactors", "gateway", "gateways",
                     "presenter", "presenters", "boundary"}
    clean_hits = all_names & clean_signals
    if clean_hits:
        signals.append(ArchSignal(
            description=f"clean_arch_packages:{sorted(clean_hits)}",
            style=ArchitectureStyle.CLEAN,
            weight=min(1.0, len(clean_hits) * 0.35),
        ))

    # ── DDD ───────────────────────────────────────────────────────────────
    ddd_signals = {"domain", "aggregate", "aggregates", "valueobject",
                   "valueobjects", "domainservice", "domainservices",
                   "domainmodel", "domainmodels", "domainevents",
                   "domainevent", "bounded", "context", "application",
                   "infrastructure"}
    ddd_hits = all_names & ddd_signals
    if len(ddd_hits) >= 2:
        signals.append(ArchSignal(
            description=f"ddd_packages:{sorted(ddd_hits)}",
            style=ArchitectureStyle.DDD,
            weight=min(1.0, len(ddd_hits) * 0.2),
        ))

    # ── CQRS ─────────────────────────────────────────────────────────────
    cqrs_signals = {"command", "commands", "query", "queries", "handler",
                    "handlers", "commandhandler", "queryhandler",
                    "commandbus", "querybus", "eventstore"}
    cqrs_hits = all_names & cqrs_signals
    if len(cqrs_hits) >= 2:
        signals.append(ArchSignal(
            description=f"cqrs_packages:{sorted(cqrs_hits)}",
            style=ArchitectureStyle.CQRS,
            weight=min(1.0, len(cqrs_hits) * 0.3),
        ))

    # ── Event-Driven ──────────────────────────────────────────────────────
    event_signals = {"event", "events", "listener", "listeners",
                     "consumer", "consumers", "producer", "producers",
                     "publisher", "publishers", "subscriber", "subscribers",
                     "handler", "handlers"}
    event_hits = all_names & event_signals
    if len(event_hits) >= 2:
        signals.append(ArchSignal(
            description=f"event_driven_packages:{sorted(event_hits)}",
            style=ArchitectureStyle.EVENT_DRIVEN,
            weight=min(0.8, len(event_hits) * 0.15),
        ))

    # ── Plugin/Extension ─────────────────────────────────────────────────
    plugin_signals = {"plugin", "plugins", "extension", "extensions",
                      "module", "modules", "spi"}
    plugin_hits = all_names & plugin_signals
    if len(plugin_hits) >= 2:
        signals.append(ArchSignal(
            description=f"plugin_packages:{sorted(plugin_hits)}",
            style=ArchitectureStyle.PLUGIN_BASED,
            weight=min(0.7, len(plugin_hits) * 0.25),
        ))

    return signals


# ---------------------------------------------------------------------------
# ── SIGNAL PROVIDER: Build file content analysis ─────────────────────────────
# ---------------------------------------------------------------------------

@arch_signal_provider("build_file_analysis")
def _build_file_signals(root: Path) -> list[ArchSignal]:
    signals: list[ArchSignal] = []

    # Multi-module Maven → Monolith or multi-module Microservices
    pom = root / "pom.xml"
    if pom.exists():
        text = _read(pom)
        module_count = text.count("<module>")
        if module_count > 1:
            signals.append(ArchSignal(
                description=f"maven_multi_module:{module_count}_modules",
                style=ArchitectureStyle.MONOLITHIC,
                weight=0.5,
            ))
            signals.append(ArchSignal(
                description=f"maven_multi_module:{module_count}_modules_ms",
                style=ArchitectureStyle.MICROSERVICES,
                weight=0.4,
            ))

        # Axon Framework → CQRS / Event Sourcing
        if "axon" in text.lower():
            signals.append(ArchSignal(
                description="axon_framework:cqrs_event_sourcing",
                style=ArchitectureStyle.CQRS,
                weight=0.9,
            ))
            signals.append(ArchSignal(
                description="axon_framework:event_driven",
                style=ArchitectureStyle.EVENT_DRIVEN,
                weight=0.8,
            ))

        # Spring Cloud → Microservices
        if "spring-cloud" in text.lower():
            signals.append(ArchSignal(
                description="spring_cloud:microservices_indicator",
                style=ArchitectureStyle.MICROSERVICES,
                weight=0.7,
            ))

    return signals


# ---------------------------------------------------------------------------
# ── SIGNAL PROVIDER: Kubernetes / Docker Compose topology ───────────────────
# ---------------------------------------------------------------------------

@arch_signal_provider("deployment_topology")
def _deployment_topology_signals(root: Path) -> list[ArchSignal]:
    signals: list[ArchSignal] = []

    compose_files = list(root.rglob("docker-compose*.yml")) + \
                    list(root.rglob("docker-compose*.yaml"))
    if compose_files:
        try:
            import yaml
            for cf in compose_files:
                data = yaml.safe_load(_read(cf))
                if isinstance(data, dict):
                    svc_count = len(data.get("services") or {})
                    if svc_count > 3:
                        signals.append(ArchSignal(
                            description=f"docker_compose:{svc_count}_services",
                            style=ArchitectureStyle.MICROSERVICES,
                            weight=min(0.8, svc_count * 0.1),
                        ))
        except (OSError, yaml.YAMLError) as exc:
            log.debug("arch_compose_scan_failed", path=str(cf), error=str(exc))

    # Multiple Helm charts or K8s deployment files → microservices
    helm_charts = list(root.rglob("Chart.yaml"))
    if len(helm_charts) > 1:
        signals.append(ArchSignal(
            description=f"multiple_helm_charts:{len(helm_charts)}",
            style=ArchitectureStyle.MICROSERVICES,
            weight=min(0.9, len(helm_charts) * 0.15),
        ))

    # Serverless
    for f in root.rglob("serverless.yml"):
        signals.append(ArchSignal(
            description="serverless_framework_config",
            style=ArchitectureStyle.SERVERLESS,
            weight=1.0,
        ))
    for f in root.rglob("template.yaml"):
        text = _read(f)
        if "AWSTemplateFormatVersion" in text or "Transform: AWS::Serverless" in text:
            signals.append(ArchSignal(
                description="aws_sam_template",
                style=ArchitectureStyle.SERVERLESS,
                weight=1.0,
            ))

    return signals


# ---------------------------------------------------------------------------
# ── SIGNAL PROVIDER: Source code patterns ───────────────────────────────────
# ---------------------------------------------------------------------------

@arch_signal_provider("source_code_patterns")
def _source_code_signals(root: Path) -> list[ArchSignal]:
    signals: list[ArchSignal] = []

    # Java: Scan a representative sample of files
    java_files = list(root.rglob("*.java"))[:80]
    cqrs_annotations = {
        "@CommandHandler", "@QueryHandler", "@EventHandler",
        "@EventSourcingHandler", "@AggregateIdentifier",
    }
    ddd_annotations = {
        "@Aggregate", "@AggregateRoot", "@DomainService",
        "@ValueObject", "@DomainEvent",
    }
    event_annotations = {
        "@KafkaListener", "@EventListener", "@RabbitListener",
        "@SqsListener", "@JmsListener",
    }
    hexagonal_annotations = {
        "@UseCase", "@InputPort", "@OutputPort", "@DrivingAdapter",
        "@DrivenAdapter",
    }

    found_cqrs: set[str] = set()
    found_ddd: set[str] = set()
    found_events: set[str] = set()
    found_hex: set[str] = set()

    for f in java_files:
        text = _read(f)
        found_cqrs  |= {a for a in cqrs_annotations  if a in text}
        found_ddd   |= {a for a in ddd_annotations   if a in text}
        found_events|= {a for a in event_annotations if a in text}
        found_hex   |= {a for a in hexagonal_annotations if a in text}

    if found_cqrs:
        signals.append(ArchSignal(
            description=f"cqrs_annotations:{sorted(found_cqrs)}",
            style=ArchitectureStyle.CQRS,
            weight=min(1.0, len(found_cqrs) * 0.35),
        ))
    if found_ddd:
        signals.append(ArchSignal(
            description=f"ddd_annotations:{sorted(found_ddd)}",
            style=ArchitectureStyle.DDD,
            weight=min(1.0, len(found_ddd) * 0.4),
        ))
    if found_events:
        signals.append(ArchSignal(
            description=f"event_annotations:{sorted(found_events)}",
            style=ArchitectureStyle.EVENT_DRIVEN,
            weight=min(0.9, len(found_events) * 0.3),
        ))
    if found_hex:
        signals.append(ArchSignal(
            description=f"hexagonal_annotations:{sorted(found_hex)}",
            style=ArchitectureStyle.HEXAGONAL,
            weight=min(1.0, len(found_hex) * 0.5),
        ))

    # Python: DDD-style patterns in source
    py_files = list(root.rglob("*.py"))[:80]
    py_ddd_patterns = [
        (r"class\s+\w+Aggregate", ArchitectureStyle.DDD, 0.7),
        (r"class\s+\w+ValueObject", ArchitectureStyle.DDD, 0.7),
        (r"class\s+\w+Repository", ArchitectureStyle.DDD, 0.4),
        (r"class\s+\w+Command", ArchitectureStyle.CQRS, 0.6),
        (r"class\s+\w+Query", ArchitectureStyle.CQRS, 0.5),
        (r"class\s+\w+Handler", ArchitectureStyle.CQRS, 0.4),
        (r"@router\.|app\.include_router", ArchitectureStyle.LAYERED, 0.3),
    ]
    for f in py_files:
        text = _read(f)
        for pattern, style, weight in py_ddd_patterns:
            if re.search(pattern, text):
                signals.append(ArchSignal(
                    description=f"python_pattern:{pattern}",
                    style=style,
                    weight=weight,
                ))
                break  # one signal per pattern per file is enough

    return signals


# ---------------------------------------------------------------------------
# ── SIGNAL PROVIDER: README / documentation ─────────────────────────────────
# ---------------------------------------------------------------------------

@arch_signal_provider("readme_docs")
def _readme_signals(root: Path) -> list[ArchSignal]:
    signals: list[ArchSignal] = []
    for fname in ("README.md", "README.rst", "README.adoc", "ARCHITECTURE.md"):
        f = root / fname
        if not f.exists():
            continue
        text = _read(f).lower()
        arch_keywords: list[tuple[str, ArchitectureStyle, float]] = [
            ("hexagonal", ArchitectureStyle.HEXAGONAL, 0.6),
            ("ports and adapters", ArchitectureStyle.HEXAGONAL, 0.7),
            ("clean architecture", ArchitectureStyle.CLEAN, 0.7),
            ("domain driven", ArchitectureStyle.DDD, 0.6),
            ("ddd", ArchitectureStyle.DDD, 0.4),
            ("cqrs", ArchitectureStyle.CQRS, 0.7),
            ("event sourcing", ArchitectureStyle.CQRS, 0.6),
            ("event-driven", ArchitectureStyle.EVENT_DRIVEN, 0.5),
            ("microservice", ArchitectureStyle.MICROSERVICES, 0.5),
            ("monolith", ArchitectureStyle.MONOLITHIC, 0.6),
            ("serverless", ArchitectureStyle.SERVERLESS, 0.7),
            ("plugin", ArchitectureStyle.PLUGIN_BASED, 0.5),
        ]
        for keyword, style, weight in arch_keywords:
            if keyword in text:
                signals.append(ArchSignal(
                    description=f"readme_keyword:{keyword}",
                    style=style,
                    weight=weight,
                ))

    return signals


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def _score_signals(signals: list[ArchSignal]) -> dict[ArchitectureStyle, float]:
    """Aggregate signal weights per style (capped at 1.0)."""
    scores: dict[ArchitectureStyle, float] = {}
    for sig in signals:
        scores[sig.style] = min(1.0, scores.get(sig.style, 0.0) + sig.weight)
    return scores


def _evidence_from_signal(sig: ArchSignal, source_file: str = "") -> ArchEvidence:
    return ArchEvidence(
        signal=sig.description,
        source_file=source_file,
        style=sig.style,
        confidence=sig.weight,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ArchitectureDiscoveryEngine:
    """Infers architecture style from repository evidence.

    Extending to a new architecture pattern:
    1. Add the value to ``ArchitectureStyle`` enum.
    2. Add signals in a ``@arch_signal_provider`` decorated function.
    3. No other changes are required.
    """

    def discover(self, repo_root: Path) -> ArchitectureModel:
        """Run all signal providers and derive the architecture style for one repo."""
        all_signals: list[ArchSignal] = []

        for name, provider in _SIGNAL_PROVIDERS:
            try:
                new_signals = provider(repo_root)
                all_signals.extend(new_signals)
            except Exception as exc:
                log.warning("arch_signal_provider_failed", provider=name, error=str(exc))

        scores = _score_signals(all_signals)
        if not scores:
            model = ArchitectureModel(primary_style=ArchitectureStyle.UNKNOWN)
            return model

        # Build sorted list of (style, score) descending
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        primary_style, primary_score = ranked[0]
        secondary = [s for s, sc in ranked[1:] if sc >= 0.3]

        # Determine layer packages for graph enrichment
        layer_packages = _extract_layer_packages(repo_root)

        # DDD-specific flags
        has_domain = "domain" in _dir_names(repo_root)
        has_app = "application" in _dir_names(repo_root)
        has_infra = "infrastructure" in _dir_names(repo_root)
        has_ports = bool({"port", "ports", "adapter", "adapters"} & _dir_names(repo_root))

        # CQRS flags
        all_dirs = _dir_names(repo_root)
        has_cmd = bool({"command", "commands"} & all_dirs)
        has_qry = bool({"query", "queries"} & all_dirs)
        has_evtsrc = "eventstore" in all_dirs

        evidence = [_evidence_from_signal(s) for s in all_signals]

        model = ArchitectureModel(
            primary_style=primary_style,
            secondary_styles=secondary,
            layer_packages=layer_packages,
            has_domain_layer=has_domain,
            has_application_layer=has_app,
            has_infrastructure_layer=has_infra,
            has_ports_adapters=has_ports,
            has_command_handlers=has_cmd,
            has_query_handlers=has_qry,
            has_event_sourcing=has_evtsrc,
            event_driven_confidence=scores.get(ArchitectureStyle.EVENT_DRIVEN, 0.0),
            evidence=evidence,
            confidence=round(primary_score, 3),
        )

        log.info(
            "arch_discovery_complete",
            repo=str(repo_root),
            primary=primary_style.value,
            confidence=model.confidence,
            secondary=[s.value for s in secondary],
        )
        return model


def _extract_layer_packages(root: Path) -> dict[str, list[str]]:
    """Map well-known layer names to discovered packages."""
    layer_map: dict[str, list[str]] = {}
    layer_keywords = [
        "controller", "service", "repository", "entity", "dto",
        "mapper", "domain", "application", "infrastructure",
        "port", "adapter", "command", "query", "event",
        "handler", "config", "security", "exception",
    ]
    for f in root.rglob("*.java"):
        m = re.search(r"^package\s+([\w.]+);", f.read_text(encoding="utf-8",
                                                            errors="replace"), re.MULTILINE)
        if not m:
            continue
        pkg = m.group(1)
        for keyword in layer_keywords:
            if keyword in pkg.lower():
                layer_map.setdefault(keyword, [])
                if pkg not in layer_map[keyword]:
                    layer_map[keyword].append(pkg)

    return layer_map
