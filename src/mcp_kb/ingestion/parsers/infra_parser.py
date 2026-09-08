"""Infra-as-code parser: Dockerfile / Kubernetes manifests / Kustomize as
first-class graph nodes (Phase 2), mirroring codebase-memory-mcp's
`pass_infrascan`/`pass_k8s`.

Dockerfiles are parsed with a real line-oriented instruction tokenizer
(handling comments, quoting and backslash line-continuations per the
Dockerfile reference grammar) rather than regex-over-whole-file, so
multi-stage builds and continued instructions are structurally correct.
Kubernetes/Kustomize manifests are already parsed into a structured dict by
PyYAML; container images are extracted by walking that structure (covering
Deployment/StatefulSet/DaemonSet/Job/CronJob/Pod/initContainers/ephemeral
containers) instead of a regex scan of the raw YAML text.
"""
from __future__ import annotations

from typing import Any

import yaml

from ...models import EdgeType, GraphEdge, GraphNode, NodeType, ParseResult, SourceFile
from .base import Parser, make_node_id


class DockerfileInstruction:
    """One logical Dockerfile instruction (continuations already joined)."""

    __slots__ = ("directive", "arguments", "line_no")

    def __init__(self, directive: str, arguments: str, line_no: int) -> None:
        self.directive = directive.upper()
        self.arguments = arguments.strip()
        self.line_no = line_no


def parse_dockerfile(text: str) -> list[DockerfileInstruction]:
    """Tokenize a Dockerfile into instructions per the Docker reference grammar:
    one instruction per logical line, `#` comments, and trailing `\\`
    continuations that join the next physical line before re-tokenizing.
    """
    instructions: list[DockerfileInstruction] = []
    pending = ""
    pending_start_line = 0
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.rstrip("\n").strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            pending_start_line = line_no
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending += stripped[:-1] + " "
            continue
        pending += stripped
        directive, _, arguments = pending.partition(" ")
        if directive:
            instructions.append(DockerfileInstruction(directive, arguments, pending_start_line))
        pending = ""
    return instructions


class InfraParser(Parser):
    """Parses Dockerfiles and Kubernetes/Kustomize YAML manifests into InfraResource nodes."""

    def parse(self, source: SourceFile) -> ParseResult:
        """Return InfraResource nodes (+ DEPENDS_ON base-image edges) for one infra file."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)
        name_lower = source.rel_path.lower()

        if "dockerfile" in name_lower:
            self._parse_dockerfile(source, service, text, result)
        elif name_lower.endswith((".yml", ".yaml")):
            self._parse_k8s_yaml(source, service, text, result)
        return result

    def _parse_dockerfile(self, source: SourceFile, service: str, text: str,
                          result: ParseResult) -> None:
        instructions = parse_dockerfile(text)
        # Multi-stage builds: `FROM <image> [AS <stage>]` — one stage per FROM.
        stages: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for instr in instructions:
            if instr.directive == "FROM":
                parts = instr.arguments.split()
                image = parts[0] if parts else ""
                stage_name = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
                current = {"image": image, "stage": stage_name, "ports": []}
                stages.append(current)
            elif instr.directive == "EXPOSE" and current is not None:
                current["ports"].extend(p.split("/")[0] for p in instr.arguments.split()
                                        if p.split("/")[0].isdigit())

        if not stages:
            return
        stage_names = {s["stage"] for s in stages if s["stage"]}
        node_id = make_node_id("infra", service, source.rel_path)
        result.nodes.append(GraphNode(
            id=node_id, type=NodeType.INFRA_RESOURCE, name=source.rel_path, service=service,
            attributes={"kind": "Dockerfile", "stages": sorted(stage_names),
                       "base_images": [s["image"] for s in stages],
                       "ports": sorted({p for s in stages for p in s["ports"]})},
        ))
        for stage in stages:
            if not stage["image"] or stage["image"] in stage_names:
                continue  # `FROM <earlier-stage>` refers back into this file, not an external image
            image_id = make_node_id("infra", "_shared", f"image:{stage['image']}")
            result.nodes.append(GraphNode(id=image_id, type=NodeType.INFRA_RESOURCE,
                                          name=stage["image"], service=None,
                                          attributes={"kind": "image"}))
            result.edges.append(GraphEdge(src=node_id, dst=image_id, type=EdgeType.DEPENDS_ON,
                                         attributes={"stage": stage["stage"]}))

    # Pod-spec-bearing fields across every workload kind that embeds one,
    # keyed by manifest `kind` (structural traversal, not a text regex).
    _POD_SPEC_PATHS: dict[str, tuple[str, ...]] = {
        "Deployment": ("spec", "template", "spec"),
        "StatefulSet": ("spec", "template", "spec"),
        "DaemonSet": ("spec", "template", "spec"),
        "ReplicaSet": ("spec", "template", "spec"),
        "Job": ("spec", "template", "spec"),
        "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
        "Pod": ("spec",),
    }

    def _dig(self, doc: dict, path: tuple[str, ...]) -> dict | None:
        node: Any = doc
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node if isinstance(node, dict) else None

    def _extract_images(self, pod_spec: dict | None) -> list[str]:
        """Walk a PodSpec's container/initContainer/ephemeralContainer lists for images."""
        if not pod_spec:
            return []
        images: list[str] = []
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for container in pod_spec.get(key) or []:
                if isinstance(container, dict) and container.get("image"):
                    images.append(container["image"])
        return images

    def _parse_k8s_yaml(self, source: SourceFile, service: str, text: str,
                        result: ParseResult) -> None:
        try:
            docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except yaml.YAMLError:
            return
        for doc in docs:
            kind = doc.get("kind")
            if not kind:
                continue
            metadata = doc.get("metadata") or {}
            name = metadata.get("name") or source.rel_path
            node_id = make_node_id("infra", service, f"{kind}:{name}:{source.rel_path}")

            pod_spec = self._dig(doc, self._POD_SPEC_PATHS.get(kind, ()))
            images = (self._extract_images(pod_spec) if pod_spec
                     else self._extract_images(doc.get("spec")))

            result.nodes.append(GraphNode(
                id=node_id, type=NodeType.INFRA_RESOURCE, name=f"{kind}/{name}", service=service,
                attributes={"kind": kind, "file": source.rel_path, "images": images,
                           "namespace": metadata.get("namespace")},
            ))
            for image in images:
                image_id = make_node_id("infra", "_shared", f"image:{image}")
                result.nodes.append(GraphNode(id=image_id, type=NodeType.INFRA_RESOURCE,
                                              name=image, service=None, attributes={"kind": "image"}))
                result.edges.append(GraphEdge(src=node_id, dst=image_id, type=EdgeType.DEPENDS_ON))

