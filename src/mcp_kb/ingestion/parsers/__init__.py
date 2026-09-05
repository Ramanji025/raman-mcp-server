"""Language- and format-specific parsers producing graph nodes + chunks."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import Settings
from ...models import ContentType, ParseResult, SourceFile
from .base import Parser
from .java_parser import JavaParser
from .markdown_parser import MarkdownParser
from .openapi_parser import OpenApiParser
from .pom_parser import PomParser
from .schema_parser import FlywayParser, LiquibaseParser
from .yaml_parser import SpringYamlParser

if TYPE_CHECKING:
    from ..repository_intelligence.models import ProjectKnowledgeModel


class ParserRegistry:
    """Dispatches a :class:`SourceFile` to the correct parser by content type.

    Call :meth:`set_project_model` once per repository before parsing begins
    so every parser receives the pre-computed ``ProjectKnowledgeModel`` for
    technology enrichment context.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._by_type: dict[ContentType, Parser] = {
            ContentType.JAVA: JavaParser(settings),
            ContentType.MAVEN: PomParser(settings),
            ContentType.SPRING_YAML: SpringYamlParser(settings),
            ContentType.LIQUIBASE: LiquibaseParser(settings),
            ContentType.FLYWAY: FlywayParser(settings),
            ContentType.OPENAPI: OpenApiParser(settings),
            ContentType.DOCS: MarkdownParser(settings, ContentType.DOCS),
            ContentType.INCIDENTS: MarkdownParser(settings, ContentType.INCIDENTS),
        }

    def set_project_model(self, model: ProjectKnowledgeModel | None) -> None:
        """Broadcast the project knowledge model to every registered parser."""
        for parser in self._by_type.values():
            parser.set_project_model(model)

    def parse(self, source: SourceFile) -> ParseResult:
        """Dispatch to the registered parser for `source`'s content type."""
        parser = self._by_type.get(source.content_type)
        if parser is None:
            return ParseResult()
        try:
            return parser.parse(source)
        except Exception as exc:  # a bad file must never abort a whole run
            from ...logging import get_logger

            get_logger(__name__).warning(
                "parse_failed", repo=source.repo, path=source.rel_path, error=str(exc)
            )
            return ParseResult()


__all__ = [
    "FlywayParser",
    "JavaParser",
    "LiquibaseParser",
    "MarkdownParser",
    "OpenApiParser",
    "Parser",
    "ParserRegistry",
    "PomParser",
    "SpringYamlParser",
]
