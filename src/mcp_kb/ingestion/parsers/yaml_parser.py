"""Spring ``application.yml`` parser: config nodes, datasource + Kafka topics."""
from __future__ import annotations

import re
from typing import Any

import yaml

from ...models import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    ParseResult,
    SourceFile,
)
from .base import Parser, make_node_id

# Values matching these patterns should be flagged as potential secrets.
_SECRET_PATTERNS = re.compile(
    r"(password|secret|token|apikey|api_key|credentials|private.?key)",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"^\$\{.+\}$|^\${.+:.*}$|^ENC\(")


class SpringYamlParser(Parser):
    """Parses Spring Boot application.yml/properties files into Config graph nodes."""

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse a Spring Boot application.yml/properties file into Config graph nodes."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)
        try:
            docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except yaml.YAMLError:
            return result

        merged: dict[str, Any] = {}
        for d in docs:
            _deep_merge(merged, d)

        app_name = _dig(merged, "spring", "application", "name") or service
        cfg_id = make_node_id("config", service, source.rel_path)
        attrs: dict[str, Any] = {"application_name": app_name, "file": source.rel_path}

        ds_url = _dig(merged, "spring", "datasource", "url")
        if ds_url:
            attrs["datasource_url"] = ds_url
        server_port = _dig(merged, "server", "port")
        if server_port is not None:
            attrs["server_port"] = server_port

        # Flatten all leaf properties for audit.
        flat_props = _flatten(merged)
        secret_keys: list[str] = []
        hardcoded_secrets: list[str] = []

        for key, val in flat_props.items():
            is_secret_key = bool(_SECRET_PATTERNS.search(key))
            str_val = str(val) if val is not None else ""
            is_placeholder = bool(_PLACEHOLDER.match(str_val))

            if is_secret_key:
                secret_keys.append(key)
                if str_val and not is_placeholder:
                    hardcoded_secrets.append(key)

            # Emit individual ConfigProperty nodes for important keys.
            prop_id = make_node_id("prop", service, key)
            result.nodes.append(
                GraphNode(id=prop_id, type=NodeType.CONFIG_PROPERTY, name=key,
                          service=service,
                          attributes={
                              "value": str_val if not is_secret_key else "<redacted>",
                              "is_secret": is_secret_key,
                              "is_placeholder": is_placeholder,
                              "hardcoded": is_secret_key and not is_placeholder,
                              "file": source.rel_path,
                          })
            )
            result.edges.append(
                GraphEdge(src=cfg_id, dst=prop_id, type=EdgeType.DECLARED_IN)
            )

        attrs["property_count"] = len(flat_props)
        attrs["secret_keys"] = secret_keys
        attrs["hardcoded_secrets"] = hardcoded_secrets
        attrs["all_properties"] = {
            k: ("<redacted>" if _SECRET_PATTERNS.search(k) else str(v))
            for k, v in flat_props.items()
        }

        result.nodes.append(
            GraphNode(id=cfg_id, type=NodeType.CONFIG, name=app_name,
                      service=service, attributes=attrs)
        )
        result.edges.append(
            GraphEdge(src=make_node_id("service", service, service), dst=cfg_id,
                      type=EdgeType.DECLARED_IN)
        )

        for topic in _find_topics(merged):
            topic_id = make_node_id("topic", "_shared", topic)
            result.nodes.append(
                GraphNode(id=topic_id, type=NodeType.TOPIC, name=topic,
                          service=None, attributes={"source": "config"})
            )

        result.chunks.extend(
            self._chunk_text(source, text, collection="architecture",
                             metadata={"artifact": "application.yml",
                                       "service": service, "application_name": app_name})
        )
        return result


def _deep_merge(base: dict, other: dict) -> None:
    for k, v in other.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _dig(data: Any, *keys: str) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested YAML dict to dot-separated keys."""
    result: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict,)):
                result.update(_flatten(v, full_key))
            elif isinstance(v, list):
                result[full_key] = v  # keep lists as-is
            else:
                result[full_key] = v
    return result


def _find_topics(data: Any) -> set[str]:
    """Collect any scalar value under a key containing 'topic'."""
    found: set[str] = set()

    def walk(node: Any, parent_key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)
        elif isinstance(node, str) and "topic" in parent_key.lower():
            if node and " " not in node:
                found.add(node)

    walk(data)
    return found
