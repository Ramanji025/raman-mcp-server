"""Repository Classifier.

Determines *what kind* of repository this is — Microservice, Library,
Gateway, AuthService, etc. — from accumulated detection evidence.

Classification is a weighted scoring problem: every signal contributes
to one or more ``RepositoryType`` scores, and the highest-scoring type
wins with a confidence value.

Extending: register a new ``@classifier`` function.  The scorer picks it up
automatically — no pipeline changes are needed.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ...logging import get_logger
from .models import ArchitectureStyle, RepositoryType
from .tech_detector import DetectionContext

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Classifier registry
# ---------------------------------------------------------------------------

ClassifierFn = Callable[["ClassificationContext"], list[tuple[RepositoryType, float, str]]]
# Each tuple: (type, score_delta, reason)

_CLASSIFIERS: list[tuple[str, ClassifierFn]] = []


def classifier(name: str) -> Callable[[ClassifierFn], ClassifierFn]:
    """Register a classifier function."""
    def _wrap(fn: ClassifierFn) -> ClassifierFn:
        _CLASSIFIERS.append((name, fn))
        return fn
    return _wrap


# ---------------------------------------------------------------------------
# Context passed to all classifiers
# ---------------------------------------------------------------------------

class ClassificationContext:
    """Shared context (repo root, detected tech, architecture style) passed to all classifiers."""

    def __init__(
        self,
        repo_root: Path,
        tech_ctx: DetectionContext,
        arch_style: ArchitectureStyle,
    ) -> None:
        self.repo_root = repo_root
        self.tech = tech_ctx
        self.arch_style = arch_style
        self._dir_names: set[str] | None = None
        self._file_names: set[str] | None = None

    @property
    def dirs(self) -> set[str]:
        """Lowercased set of every directory name under the repo (cached)."""
        if self._dir_names is None:
            self._dir_names = set()
            try:
                for p in self.repo_root.rglob("*"):
                    if p.is_dir() and not p.name.startswith("."):
                        self._dir_names.add(p.name.lower())
            except PermissionError:
                pass
        return self._dir_names

    @property
    def files(self) -> set[str]:
        """Lowercased set of every file name under the repo (cached)."""
        if self._file_names is None:
            self._file_names = set()
            try:
                for p in self.repo_root.rglob("*"):
                    if p.is_file():
                        self._file_names.add(p.name.lower())
            except PermissionError:
                pass
        return self._file_names

    def has_file(self, *names: str) -> bool:
        """Return True if any of `names` (case-insensitive) exists as a file in the repo."""
        return bool(self.files & set(n.lower() for n in names))

    def has_dir(self, *names: str) -> bool:
        """Return True if any of `names` (case-insensitive) exists as a directory in the repo."""
        return bool(self.dirs & set(n.lower() for n in names))

    def _sample_java_text(self) -> str:
        """Concatenate text of up to 50 .java files for annotation scanning."""
        parts: list[str] = []
        for f in list(self.repo_root.rglob("*.java"))[:50]:
            try:
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(parts)

    def _sample_source_text(self, extensions: tuple[str, ...] = (".java", ".py", ".cs", ".ts")) -> str:
        parts: list[str] = []
        for ext in extensions:
            for f in list(self.repo_root.rglob(f"*{ext}"))[:20]:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Library ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("library")
def _classify_library(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []

    # Has no main application class / entry point
    has_main = ctx.has_dir("controller", "controllers") or \
               any(f.endswith("application.java") or f.endswith("starter.java")
                   for f in ctx.files)

    # Has typical library indicators
    t = ctx.tech.tech_stack.primary_language.value.lower()
    if "java" in t or "kotlin" in t:
        # pom with packaging jar and no SpringBootApplication
        pom = ctx.repo_root / "pom.xml"
        if pom.exists():
            text = pom.read_text(encoding="utf-8", errors="replace")
            if "<packaging>jar</packaging>" in text and "spring-boot-maven-plugin" not in text:
                scores.append((RepositoryType.LIBRARY, 0.6, "pom:jar_no_spring_boot_plugin"))
        # has "starter" in repo name or artifact id
        for dep in ctx.tech.dependencies:
            for d in dep.direct:
                if "starter" in d.lower() and "auto-configure" in d.lower():
                    scores.append((RepositoryType.LIBRARY, 0.4, f"starter_autoconfigure:{d}"))

    if ctx.has_dir("lib", "library") and not has_main:
        scores.append((RepositoryType.LIBRARY, 0.5, "lib_dir_no_main"))

    if ctx.has_file("setup.py", "setup.cfg", "pyproject.toml") and \
            not ctx.has_file("manage.py", "app.py", "main.py", "server.py"):
        scores.append((RepositoryType.LIBRARY, 0.6, "python_package_no_entry_point"))

    if ctx.has_file("*.csproj") and not ctx.has_dir("controllers", "endpoints"):
        # .NET class library
        for f in ctx.repo_root.rglob("*.csproj"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            if "classlib" in txt.lower() or "OutputType" not in txt:
                scores.append((RepositoryType.LIBRARY, 0.7, "dotnet_classlib"))
                break

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Gateway ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("gateway")
def _classify_gateway(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("gateway", "api-gateway", "proxy", "edge")):
        scores.append((RepositoryType.GATEWAY, 0.7, f"repo_name:{repo_name}"))

    if "spring-cloud-gateway" in " ".join(ctx.tech.tech_stack.raw_dependencies).lower():
        scores.append((RepositoryType.GATEWAY, 0.9, "spring_cloud_gateway_dep"))

    if "spring-cloud-netflix-zuul" in " ".join(ctx.tech.tech_stack.raw_dependencies).lower():
        scores.append((RepositoryType.GATEWAY, 0.9, "zuul_dep"))

    if ctx.has_file("application.yml", "application.yaml"):
        for f in ctx.repo_root.rglob("application*.yml"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                if re.search(r"spring\.cloud\.gateway\.routes", text, re.IGNORECASE):
                    scores.append((RepositoryType.GATEWAY, 0.95, "spring_cloud_gateway_routes_config"))
                if re.search(r"zuul\.routes", text, re.IGNORECASE):
                    scores.append((RepositoryType.GATEWAY, 0.95, "zuul_routes_config"))
            except OSError:
                pass

    # Kong / NGINX / Envoy config files
    for f in ctx.repo_root.rglob("*.yaml"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace").lower()
            if "kong:" in text or "upstream:" in text and "proxy_pass" in text:
                scores.append((RepositoryType.GATEWAY, 0.8, f"kong_or_nginx_config:{f.name}"))
                break
        except OSError:
            pass

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Authentication Service ──────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("auth_service")
def _classify_auth(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("auth", "identity", "iam", "sso", "login",
                                       "keycloak", "oauth", "jwt")):
        scores.append((RepositoryType.AUTH_SERVICE, 0.7, f"repo_name:{repo_name}"))

    ts = ctx.tech.tech_stack
    if ts.security_technologies:
        # Multiple security frameworks = dedicated auth service
        if len(ts.security_technologies) >= 2:
            scores.append((RepositoryType.AUTH_SERVICE, 0.5,
                           f"multiple_security_techs:{ts.security_technologies}"))

    src = ctx._sample_java_text()
    if src:
        auth_annotations = [
            "@EnableAuthorizationServer",
            "@EnableResourceServer",
            "@EnableWebSecurity",
            "AuthorizationServerConfigurerAdapter",
            "ResourceServerConfigurerAdapter",
            "UserDetailsService",
        ]
        hits = [a for a in auth_annotations if a in src]
        if hits:
            scores.append((RepositoryType.AUTH_SERVICE,
                           min(0.9, len(hits) * 0.2),
                           f"auth_annotations:{hits}"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Batch Application ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("batch")
def _classify_batch(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("batch", "etl", "job", "scheduler", "cron", "worker")):
        scores.append((RepositoryType.BATCH_APPLICATION, 0.6, f"repo_name:{repo_name}"))

    raw_deps = " ".join(ctx.tech.tech_stack.raw_dependencies).lower()
    if "spring-batch" in raw_deps:
        scores.append((RepositoryType.BATCH_APPLICATION, 0.9, "spring_batch_dep"))

    if "spring-quartz" in raw_deps or "quartz" in raw_deps:
        scores.append((RepositoryType.BATCH_APPLICATION, 0.6, "quartz_scheduler_dep"))

    src = ctx._sample_java_text()
    if src and ("@EnableBatchProcessing" in src or
                "@Scheduled" in src or
                "ItemReader" in src or
                "ItemWriter" in src):
        scores.append((RepositoryType.BATCH_APPLICATION, 0.8, "spring_batch_annotations"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Frontend Application ────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("frontend")
def _classify_frontend(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    ts = ctx.tech.tech_stack
    fw = " ".join(ts.frameworks).lower()

    frontend_frameworks = {"angular", "react", "vue.js", "next.js", "nuxt.js",
                           "svelte", "ember.js", "backbone.js"}
    hits = {f for f in frontend_frameworks if f in fw}
    if hits:
        scores.append((RepositoryType.FRONTEND, 0.9, f"frontend_framework:{hits}"))

    if ctx.has_file("index.html") and ctx.has_dir("src", "public"):
        scores.append((RepositoryType.FRONTEND, 0.5, "index_html_with_src_public"))

    if ctx.has_file("angular.json"):
        scores.append((RepositoryType.FRONTEND, 0.95, "angular_json"))

    if ctx.has_file(".babelrc", "vite.config.ts", "vite.config.js", "webpack.config.js"):
        scores.append((RepositoryType.FRONTEND, 0.6, "frontend_build_config"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Infrastructure / DevOps Repository ──────────────────────────
# ---------------------------------------------------------------------------

@classifier("infrastructure")
def _classify_infra(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("infra", "infrastructure", "platform",
                                       "devops", "ops", "deploy", "helm",
                                       "terraform", "k8s", "kubernetes")):
        scores.append((RepositoryType.INFRASTRUCTURE, 0.7, f"repo_name:{repo_name}"))

    deploy = ctx.tech.deployment
    # No source language + has deployment files = infra repo
    lang = ctx.tech.tech_stack.primary_language.value
    if lang == "Unknown":
        if deploy.has_terraform or deploy.has_helm_chart or deploy.has_kubernetes_manifests:
            scores.append((RepositoryType.INFRASTRUCTURE, 0.85,
                           "no_source_language_with_infra_files"))

    if deploy.has_terraform and not ctx.has_file("pom.xml", "build.gradle",
                                                  "package.json", "requirements.txt"):
        scores.append((RepositoryType.INFRASTRUCTURE, 0.8, "terraform_only_repo"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Documentation Repository ────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("documentation")
def _classify_docs(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("docs", "documentation", "wiki",
                                       "knowledge", "runbook", "playbook")):
        scores.append((RepositoryType.DOCUMENTATION, 0.7, f"repo_name:{repo_name}"))

    md_count = len(list(ctx.repo_root.rglob("*.md")))
    rst_count = len(list(ctx.repo_root.rglob("*.rst")))
    adoc_count = len(list(ctx.repo_root.rglob("*.adoc")))
    java_count = len(list(ctx.repo_root.rglob("*.java")))
    py_count = len(list(ctx.repo_root.rglob("*.py")))

    doc_count = md_count + rst_count + adoc_count
    src_count = java_count + py_count

    if doc_count > 10 and src_count < 5:
        scores.append((RepositoryType.DOCUMENTATION, 0.85,
                       f"doc_files:{doc_count}_vs_src_files:{src_count}"))

    if ctx.has_file("mkdocs.yml", "mkdocs.yaml", "docusaurus.config.js", "conf.py"):
        scores.append((RepositoryType.DOCUMENTATION, 0.9, "doc_site_config"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Event-Driven Service ─────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("event_driven_service")
def _classify_event_driven(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    ts = ctx.tech.tech_stack

    kafka_signals = sum([
        "Apache Kafka" in ts.messaging_technologies,
        "RabbitMQ" in ts.messaging_technologies,
        ctx.arch_style == ArchitectureStyle.EVENT_DRIVEN,
    ])

    if kafka_signals >= 2:
        scores.append((RepositoryType.EVENT_DRIVEN, 0.7, "kafka_plus_event_driven_arch"))
    elif kafka_signals == 1:
        scores.append((RepositoryType.EVENT_DRIVEN, 0.4, "single_event_driven_signal"))

    src = ctx._sample_java_text()
    if src:
        listener_count = src.count("@KafkaListener") + \
                         src.count("@RabbitListener") + \
                         src.count("@EventListener")
        producer_count = src.count("@KafkaProducer") + \
                         src.count("KafkaTemplate") + \
                         src.count("RabbitTemplate")
        if listener_count + producer_count > 3:
            scores.append((RepositoryType.EVENT_DRIVEN, 0.6,
                           f"kafka_producers:{producer_count}_listeners:{listener_count}"))

    repo_name = ctx.repo_root.name.lower()
    if any(kw in repo_name for kw in ("consumer", "producer", "listener",
                                       "processor", "event", "stream", "worker")):
        scores.append((RepositoryType.EVENT_DRIVEN, 0.4, f"repo_name:{repo_name}"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Shared Framework ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("shared_framework")
def _classify_shared_framework(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("commons", "common", "shared", "core",
                                       "framework", "starter", "base", "parent")):
        scores.append((RepositoryType.SHARED_FRAMEWORK, 0.6, f"repo_name:{repo_name}"))

    if ctx.has_file("pom.xml"):
        pom = ctx.repo_root / "pom.xml"
        try:
            text = pom.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "<packaging>pom</packaging>" in text:
            scores.append((RepositoryType.SHARED_FRAMEWORK, 0.7, "pom_packaging_pom"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Microservice ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("microservice")
def _classify_microservice(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    ts = ctx.tech.tech_stack

    # Spring Boot app with REST endpoints is a strong microservice signal
    has_spring_boot = "Spring Boot" in ts.frameworks
    has_rest = ctx.has_dir("controller", "controllers")
    has_single_app_class = len(list(ctx.repo_root.rglob("*Application.java"))) == 1

    if has_spring_boot and has_rest:
        scores.append((RepositoryType.MICROSERVICE, 0.75, "spring_boot_rest_service"))
    if has_spring_boot and has_single_app_class:
        scores.append((RepositoryType.MICROSERVICE, 0.65, "spring_boot_single_entry_point"))

    if "Spring Cloud" in ts.frameworks or "Netflix Eureka" in ts.frameworks:
        scores.append((RepositoryType.MICROSERVICE, 0.6, "spring_cloud_registered_service"))

    deploy = ctx.tech.deployment
    if deploy.has_dockerfile and has_spring_boot:
        scores.append((RepositoryType.MICROSERVICE, 0.5, "dockerized_spring_boot"))

    # FastAPI / Flask / Django service
    py_frameworks = {"FastAPI", "Flask", "Django"}
    py_hits = py_frameworks & set(ts.frameworks)
    if py_hits and deploy.has_dockerfile:
        scores.append((RepositoryType.MICROSERVICE, 0.7, f"python_service:{py_hits}"))

    # NestJS service
    if "NestJS" in ts.frameworks:
        scores.append((RepositoryType.MICROSERVICE, 0.7, "nestjs_service"))

    # ASP.NET Core service
    if "ASP.NET Core" in ts.frameworks and deploy.has_dockerfile:
        scores.append((RepositoryType.MICROSERVICE, 0.7, "aspnet_core_dockerized"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Monolith ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("monolith")
def _classify_monolith(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []

    pom = ctx.repo_root / "pom.xml"
    if pom.exists():
        text = pom.read_text(encoding="utf-8", errors="replace")
        module_count = text.count("<module>")
        if module_count > 5:
            scores.append((RepositoryType.MONOLITH, 0.6,
                           f"maven_multi_module:{module_count}"))

    if ctx.arch_style == ArchitectureStyle.MONOLITHIC:
        scores.append((RepositoryType.MONOLITH, 0.7, "arch_style:monolithic"))

    java_count = len(list(ctx.repo_root.rglob("*.java")))
    if java_count > 500 and not ctx.has_dir("microservices", "services"):
        scores.append((RepositoryType.MONOLITH, 0.5, f"large_java_codebase:{java_count}"))

    return scores


# ---------------------------------------------------------------------------
# ── CLASSIFIER: Utility Service ──────────────────────────────────────────────
# ---------------------------------------------------------------------------

@classifier("utility")
def _classify_utility(ctx: ClassificationContext) -> list[tuple[RepositoryType, float, str]]:
    scores: list[tuple[RepositoryType, float, str]] = []
    repo_name = ctx.repo_root.name.lower()

    if any(kw in repo_name for kw in ("util", "utility", "helper", "tool",
                                       "toolkit", "cli", "script")):
        scores.append((RepositoryType.UTILITY, 0.6, f"repo_name:{repo_name}"))

    # Small service with no REST but has scheduled tasks or CLIs
    ts = ctx.tech.tech_stack
    is_small = len(list(ctx.repo_root.rglob("*.java"))) < 30
    if is_small and "Spring Boot" in ts.frameworks and not ctx.has_dir("controller"):
        scores.append((RepositoryType.UTILITY, 0.5, "small_spring_boot_no_rest"))

    return scores


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class RepositoryClassifier:
    """Classifies a repository into a ``RepositoryType`` using scored signals.

    Extending: write a ``@classifier("name")`` decorated function.
    No pipeline changes are needed.
    """

    def classify(
        self,
        repo_root: Path,
        tech_ctx: DetectionContext,
        arch_style: ArchitectureStyle,
    ) -> tuple[RepositoryType, float, list[str]]:
        """Return (repo_type, confidence, evidence_list)."""
        ctx = ClassificationContext(repo_root, tech_ctx, arch_style)
        scores: dict[RepositoryType, float] = {}
        evidence: dict[RepositoryType, list[str]] = {}

        for name, fn in _CLASSIFIERS:
            try:
                results = fn(ctx)
                for rtype, delta, reason in results:
                    scores[rtype] = scores.get(rtype, 0.0) + delta
                    evidence.setdefault(rtype, []).append(reason)
            except Exception as exc:
                log.warning("classifier_failed", classifier=name, error=str(exc))

        if not scores:
            return RepositoryType.UNKNOWN, 0.0, []

        # Cap scores at 1.0
        scores = {k: min(1.0, v) for k, v in scores.items()}
        best_type = max(scores, key=lambda k: scores[k])
        best_score = scores[best_type]
        best_evidence = evidence.get(best_type, [])

        log.info(
            "repo_classified",
            repo=str(repo_root),
            type=best_type.value,
            confidence=round(best_score, 3),
            evidence=best_evidence,
        )
        return best_type, round(best_score, 3), best_evidence
