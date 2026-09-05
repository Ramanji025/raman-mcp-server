"""Database schema parsers for Liquibase (XML/YAML) and Flyway (SQL)."""
from __future__ import annotations

import re

from lxml import etree

from ...models import (
    GraphNode,
    NodeType,
    ParseResult,
    SourceFile,
)
from .base import Parser, make_node_id


class LiquibaseParser(Parser):
    """Extracts ``Table`` nodes from ``createTable`` change sets."""

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse a Liquibase changelog into Table graph nodes."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)

        table_names: set[str] = set()
        if source.rel_path.endswith((".xml",)):
            table_names |= self._from_xml(source.abs_path)
        else:  # yaml changelog
            table_names |= set(re.findall(r"tableName:\s*['\"]?([A-Za-z0-9_]+)", text))

        for table in table_names:
            columns = self._columns_for(text, table)
            result.nodes.append(
                GraphNode(
                    id=make_node_id("table", service, table),
                    type=NodeType.TABLE, name=table, service=service,
                    attributes={"columns": columns, "source": "liquibase",
                                "file": source.rel_path},
                )
            )

        result.chunks.extend(
            self._chunk_text(source, text, collection="architecture",
                             metadata={"artifact": "liquibase", "service": service,
                                       "tables": sorted(table_names)})
        )
        return result

    @staticmethod
    def _from_xml(path: str) -> set[str]:
        tables: set[str] = set()
        try:
            tree = etree.parse(path)
        except etree.XMLSyntaxError:
            return tables
        for el in tree.iter():
            local = etree.QName(el).localname if el.tag is not etree.Comment else ""
            if local in ("createTable", "dropTable", "addColumn"):
                name = el.get("tableName")
                if name:
                    tables.add(name)
        return tables

    @staticmethod
    def _columns_for(text: str, table: str) -> list[str]:
        return sorted(set(re.findall(r'column\s+name="([A-Za-z0-9_]+)"', text)))[:64]


class FlywayParser(Parser):
    """Extracts ``Table`` nodes from Flyway ``CREATE TABLE`` migrations."""

    _CREATE = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?([A-Za-z0-9_\.]+)",
        re.IGNORECASE,
    )

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse a Flyway SQL migration into Table graph nodes."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)
        for match in self._CREATE.finditer(text):
            table = match.group(1).split(".")[-1]
            result.nodes.append(
                GraphNode(
                    id=make_node_id("table", service, table),
                    type=NodeType.TABLE, name=table, service=service,
                    attributes={"source": "flyway", "file": source.rel_path},
                )
            )
        result.chunks.extend(
            self._chunk_text(source, text, collection="architecture",
                             metadata={"artifact": "flyway", "service": service})
        )
        return result
