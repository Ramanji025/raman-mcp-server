"""Adaptive Parser Selector.

Determines which parsers to activate for a repository based on its
``ProjectKnowledgeModel``, and generates dynamic content-type-to-glob
mappings that the ``RepoScanner`` can use at runtime.

Design principles
-----------------
* Every parser binding is registered via ``@parser_binding``.
* Adding a new language parser requires only registering a new binding.
* No modifications to ``IngestionPipeline``, ``RepoScanner``, or
  ``ParserRegistry`` are necessary.
* The selector also emits an *ordered* parse list so that build/config
  files are processed before source files (allowing technology context
  to influence later source parsing).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...logging import get_logger
from .models import (
    ParserSelectionModel,
    PrimaryLanguage,
    RepositoryType,
    TechStackModel,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Parser binding definition
# ---------------------------------------------------------------------------

@dataclass
class ParserBinding:
    """Maps a content type to a parser class name and activation condition."""
    content_type: str           # matches ContentType enum value (or a new one)
    parser_class: str           # fully-qualified or short class name
    glob_patterns: list[str]    # file patterns this parser handles
    parse_priority: int = 50    # lower = parse first (0 = highest)
    description: str = ""


# Condition: (TechStackModel, RepositoryType) → bool
ConditionFn = Callable[[TechStackModel, RepositoryType], bool]

@dataclass
class ConditionalBinding:
    """A parser binding gated by a condition on tech stack + repository type."""

    binding: ParserBinding
    condition: ConditionFn      # if True → activate this parser


_BINDINGS: list[ConditionalBinding] = []


def parser_binding(
    content_type: str,
    parser_class: str,
    glob_patterns: list[str],
    *,
    priority: int = 50,
    description: str = "",
) -> Callable[[ConditionFn], ConditionFn]:
    """Decorator to register a conditional parser binding.

    Usage::

        @parser_binding(
            content_type="java",
            parser_class="JavaParser",
            glob_patterns=["**/*.java"],
            priority=40,
        )
        def _java_condition(ts: TechStackModel, rt: RepositoryType) -> bool:
            return ts.primary_language == PrimaryLanguage.JAVA
    """
    def _wrap(fn: ConditionFn) -> ConditionFn:
        _BINDINGS.append(ConditionalBinding(
            binding=ParserBinding(
                content_type=content_type,
                parser_class=parser_class,
                glob_patterns=glob_patterns,
                parse_priority=priority,
                description=description,
            ),
            condition=fn,
        ))
        return fn
    return _wrap


# ---------------------------------------------------------------------------
# ── Built-in parser bindings ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@parser_binding(
    content_type="maven",
    parser_class="PomParser",
    glob_patterns=["**/pom.xml"],
    priority=5,
    description="Maven POM — build metadata, dependencies, modules",
)
def _bind_maven(ts: TechStackModel, rt: RepositoryType) -> bool:
    from .models import BuildSystem
    return BuildSystem.MAVEN in ts.build_systems


@parser_binding(
    content_type="gradle",
    parser_class="GradleParser",
    glob_patterns=["**/build.gradle", "**/build.gradle.kts", "**/settings.gradle"],
    priority=5,
    description="Gradle build file — dependencies, plugins",
)
def _bind_gradle(ts: TechStackModel, rt: RepositoryType) -> bool:
    from .models import BuildSystem
    return BuildSystem.GRADLE in ts.build_systems


@parser_binding(
    content_type="java",
    parser_class="JavaParser",
    glob_patterns=["**/*.java"],
    priority=40,
    description="Java source — controllers, services, entities, events",
)
def _bind_java(ts: TechStackModel, rt: RepositoryType) -> bool:
    return ts.primary_language in (PrimaryLanguage.JAVA, PrimaryLanguage.KOTLIN)


@parser_binding(
    content_type="spring_yaml",
    parser_class="SpringYamlParser",
    glob_patterns=[
        "**/application*.yml", "**/application*.yaml",
        "**/application*.properties",
        "**/bootstrap*.yml", "**/bootstrap*.yaml",
    ],
    priority=10,
    description="Spring Boot configuration — properties, feature flags",
)
def _bind_spring_yaml(ts: TechStackModel, rt: RepositoryType) -> bool:
    return "Spring Boot" in ts.frameworks


@parser_binding(
    content_type="openapi",
    parser_class="OpenApiParser",
    glob_patterns=[
        "**/openapi*.yaml", "**/openapi*.yml", "**/openapi*.json",
        "**/swagger*.yaml", "**/swagger*.yml", "**/swagger*.json",
        "**/api-docs*.yaml", "**/api-docs*.json",
    ],
    priority=15,
    description="OpenAPI/Swagger spec — endpoint contracts",
)
def _bind_openapi(ts: TechStackModel, rt: RepositoryType) -> bool:
    return bool(ts.doc_sources) or rt not in (
        RepositoryType.INFRASTRUCTURE, RepositoryType.DOCUMENTATION
    )


@parser_binding(
    content_type="liquibase",
    parser_class="LiquibaseParser",
    glob_patterns=[
        "**/db/changelog/**/*.xml",
        "**/db/changelog/**/*.yaml",
        "**/db/changelog/**/*.yml",
        "**/db/changelog/**/*.sql",
        "**/liquibase/**/*.xml",
    ],
    priority=20,
    description="Liquibase changelog — schema migrations",
)
def _bind_liquibase(ts: TechStackModel, rt: RepositoryType) -> bool:
    return "Liquibase" in ts.persistence_technologies


@parser_binding(
    content_type="flyway",
    parser_class="FlywayParser",
    glob_patterns=[
        "**/db/migration/**/*.sql",
        "**/flyway/**/*.sql",
        "**/migration/**/*.sql",
    ],
    priority=20,
    description="Flyway SQL migration scripts",
)
def _bind_flyway(ts: TechStackModel, rt: RepositoryType) -> bool:
    return "Flyway" in ts.persistence_technologies


@parser_binding(
    content_type="docs",
    parser_class="MarkdownParser",
    glob_patterns=["**/*.md", "**/*.mdx", "**/*.rst", "**/*.adoc"],
    priority=60,
    description="Markdown/AsciiDoc documentation",
)
def _bind_docs(_ts: TechStackModel, _rt: RepositoryType) -> bool:
    return True   # always activated


@parser_binding(
    content_type="incidents",
    parser_class="MarkdownParser",
    glob_patterns=["**/incidents/**/*.md", "**/runbooks/**/*.md",
                   "**/postmortem*.md", "**/incident*.md"],
    priority=70,
    description="Incident reports / runbooks / postmortems",
)
def _bind_incidents(_ts: TechStackModel, _rt: RepositoryType) -> bool:
    return True


# ── Future parsers (inactive until their parser class is implemented) ────────

@parser_binding(
    content_type="csharp",
    parser_class="CSharpParser",
    glob_patterns=["**/*.cs"],
    priority=40,
    description="C# source — controllers, services, entities",
)
def _bind_csharp(ts: TechStackModel, _rt: RepositoryType) -> bool:
    return ts.primary_language == PrimaryLanguage.CSHARP


@parser_binding(
    content_type="python",
    parser_class="PythonParser",
    glob_patterns=["**/*.py"],
    priority=40,
    description="Python source — routes, models, tasks",
)
def _bind_python(ts: TechStackModel, _rt: RepositoryType) -> bool:
    return ts.primary_language == PrimaryLanguage.PYTHON


@parser_binding(
    content_type="typescript",
    parser_class="TypeScriptParser",
    glob_patterns=["**/*.ts", "**/*.tsx"],
    priority=40,
    description="TypeScript source — components, services, routes",
)
def _bind_typescript(ts: TechStackModel, _rt: RepositoryType) -> bool:
    return ts.primary_language == PrimaryLanguage.TYPESCRIPT


@parser_binding(
    content_type="go",
    parser_class="GoParser",
    glob_patterns=["**/*.go"],
    priority=40,
    description="Go source — handlers, services, models",
)
def _bind_go(ts: TechStackModel, _rt: RepositoryType) -> bool:
    return ts.primary_language == PrimaryLanguage.GO


@parser_binding(
    content_type="docker",
    parser_class="DockerfileParser",
    glob_patterns=["**/Dockerfile", "**/Dockerfile.*", "**/*.dockerfile"],
    priority=25,
    description="Dockerfile — container image definition",
)
def _bind_docker(ts: TechStackModel, rt: RepositoryType) -> bool:
    # Use a simple heuristic — avoids importing DeploymentModel into selector
    return True   # always attempt; parser can no-op if file is empty


@parser_binding(
    content_type="kubernetes",
    parser_class="KubernetesParser",
    glob_patterns=["**/*.yaml", "**/*.yml"],
    priority=30,
    description="Kubernetes manifests — deployments, services, ingress",
)
def _bind_kubernetes(_ts: TechStackModel, rt: RepositoryType) -> bool:
    return rt == RepositoryType.INFRASTRUCTURE


@parser_binding(
    content_type="terraform",
    parser_class="TerraformParser",
    glob_patterns=["**/*.tf", "**/*.tfvars"],
    priority=30,
    description="Terraform HCL — infrastructure as code",
)
def _bind_terraform(_ts: TechStackModel, rt: RepositoryType) -> bool:
    return rt == RepositoryType.INFRASTRUCTURE


@parser_binding(
    content_type="npm_package",
    parser_class="NpmPackageParser",
    glob_patterns=["**/package.json"],
    priority=5,
    description="npm package.json — dependencies, scripts",
)
def _bind_npm(ts: TechStackModel, _rt: RepositoryType) -> bool:
    from .models import BuildSystem
    return BuildSystem.NPM in ts.build_systems or \
           BuildSystem.YARN in ts.build_systems or \
           BuildSystem.PNPM in ts.build_systems


@parser_binding(
    content_type="requirements_txt",
    parser_class="RequirementsParser",
    glob_patterns=["**/requirements*.txt", "**/pyproject.toml", "**/setup.py"],
    priority=5,
    description="Python requirements / pyproject — dependencies",
)
def _bind_requirements(ts: TechStackModel, _rt: RepositoryType) -> bool:
    return ts.primary_language == PrimaryLanguage.PYTHON


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class AdaptiveParserSelector:
    """Selects and orders parsers for a given repository.

    Extending to a new language:
    1.  Implement the parser class.
    2.  Register a ``@parser_binding`` decorated condition function.
    3.  No changes to ``IngestionPipeline`` or ``ParserRegistry`` are needed.
    """

    def select(
        self,
        repo_root: Path,
        tech_stack: TechStackModel,
        repo_type: RepositoryType,
    ) -> ParserSelectionModel:
        """Select which conditional parser bindings apply to this repo's tech stack/type."""
        selected_parsers: dict[str, str] = {}
        dynamic_patterns: dict[str, list[str]] = {}
        active_bindings: list[ParserBinding] = []

        for cb in _BINDINGS:
            try:
                if cb.condition(tech_stack, repo_type):
                    b = cb.binding
                    selected_parsers[b.content_type] = b.parser_class
                    dynamic_patterns[b.content_type] = b.glob_patterns
                    active_bindings.append(b)
            except Exception as exc:
                log.warning(
                    "parser_binding_condition_failed",
                    content_type=cb.binding.content_type,
                    error=str(exc),
                )

        # Sort by priority (lowest number = first)
        active_bindings.sort(key=lambda b: b.parse_priority)
        parse_order = [b.content_type for b in active_bindings]

        model = ParserSelectionModel(
            selected_parsers=selected_parsers,
            parse_order=parse_order,
            dynamic_patterns=dynamic_patterns,
        )

        log.info(
            "parser_selection_complete",
            repo=str(repo_root),
            selected=[f"{ct}→{pc}" for ct, pc in selected_parsers.items()],
        )
        return model

    @staticmethod
    def registered_bindings() -> list[dict]:
        """Return a registry summary for introspection / debugging."""
        return [
            {
                "content_type": cb.binding.content_type,
                "parser_class": cb.binding.parser_class,
                "priority": cb.binding.parse_priority,
                "description": cb.binding.description,
                "glob_patterns": cb.binding.glob_patterns,
            }
            for cb in _BINDINGS
        ]
