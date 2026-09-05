"""Offline Ollama enrichment of Java/Spring structure into Neo4j + Qdrant.

Persistence:
  * Neo4j ``KBNode`` attributes ``llm_summary``, ``llm_purpose``, ``llm_enriched``
  * Qdrant semantic collection via :func:`sync_ollama_enrichment`
Progress/resume is the graph itself (skip nodes with ``llm_enriched``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from ..config import Settings, get_settings
from ..graph.factory import get_graph_store
from ..logging import get_logger
from ..models import (
    ContentType,
    GraphNode,
    NodeType,
    SourceFile,
)
from ..semantic.enrichment_sync import sync_ollama_enrichment
from .parsers.java_parser import JavaParser

log = get_logger(__name__)

_OWNER_TYPES = {
    NodeType.CONTROLLER,
    NodeType.SERVICE_LAYER,
    NodeType.ENTITY,
    NodeType.REPOSITORY,
    NodeType.DTO,
    NodeType.EXCEPTION_TYPE,
    NodeType.TEST_CLASS,
    NodeType.CLASS,
    NodeType.INTERFACE,
}

_SKIP_DIR_NAMES = {
    ".git", "target", "build", "out", "node_modules", ".idea",
    "generated-sources", "generated-test-sources",
}

_PROMPT_PREAMBLE = """You are documenting a Java / Spring Boot compilation unit for a knowledge graph.
Return ONLY valid JSON (no markdown) with this shape:
{
  "class_summary": "2-4 sentences: business role, Spring stereotype, collaborators",
  "spring_role": "Controller|Service|Repository|Entity|DTO|Config|Client|Test|Other",
  "fields": [{"name": "fieldName", "purpose": "one sentence"}],
  "constructors": [{"name": "ClassName", "purpose": "one sentence"}],
  "methods": [{
    "name": "methodName",
    "purpose": "what it does and why",
    "parameters": [{"name": "param", "purpose": "how it is used"}],
    "annotations": [{"name": "GetMapping", "meaning": "HTTP GET on this method"}],
    "logic_blocks": [{"kind": "if", "start_line": 42, "purpose": "guard / branch meaning"}]
  }],
  "type_annotations": [{"name": "RestController", "meaning": "why this type has it"}]
}
Be specific to the source. Empty arrays are allowed. Keep each purpose to 1-2 sentences.
"""

# Methods per LLM call; keep prompts well under 4k output tokens to avoid truncation
_METHOD_BATCH_SIZE = 15


def _ollama_config(settings: Settings) -> tuple[str, str]:
    # llama.cpp and Ollama both expose the OpenAI-compatible /v1/chat/completions endpoint.
    # OLLAMA_MODEL overrides LLM_MODEL for this offline script; both come from .env.
    url = settings.chat_completions_url
    model = settings.ollama_model or settings.effective_llm_model
    return url, model


def ollama_generate(
    prompt: str,
    settings: Settings,
    *,
    max_tokens: int = 4096000,
    timeout: int = 3000,
) -> str:
    """Call the local/remote Ollama chat-completions endpoint with retry, returning raw text."""
    url, model = _ollama_config(settings)
    last_error = ""
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                    # thinking tokens are pure overhead here and can starve the JSON answer
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=timeout,
            )
            if resp.status_code == 404:
                raise SystemExit(
                    f"LLM 404 at {url}. Check OLLAMA_BASE_URL in your .env."
                )
            if resp.status_code >= 500:
                # server body usually explains the real cause (context overflow, slot busy, OOM)
                raise RuntimeError(f"{resp.status_code} server error: {resp.text[:500]}")
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            text = message.get("content") or message.get("reasoning_content") or ""
            return text.strip()
        except SystemExit:
            raise
        except Exception as exc:
            last_error = str(exc)
            log.warning("ollama_generate_retry", attempt=attempt + 1, error=last_error)
            time.sleep(5)
    raise SystemExit(f"LLM generate failed after 3 retries: {last_error}")


def _parse_json_object(text: str) -> dict[str, Any]:
    blob = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", blob, re.DOTALL)
    if fence:
        blob = fence.group(1)
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(blob[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _iter_java_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_dir.rglob("*.java"):
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return files


def _source_file(repo: str, repo_dir: Path, java_path: Path) -> SourceFile:
    rel = java_path.relative_to(repo_dir).as_posix()
    return SourceFile(
        repo=repo,
        rel_path=rel,
        abs_path=str(java_path),
        content_type=ContentType.JAVA,
        sha256="enrichment",
        size_bytes=java_path.stat().st_size,
    )


def _owner_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    return [n for n in nodes if n.type in _OWNER_TYPES]


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _merge_attrs(node: GraphNode, updates: dict[str, Any]) -> GraphNode:
    attrs = dict(node.attributes)
    attrs.update(updates)
    attrs["llm_enriched"] = True
    attrs["llm_enriched_at"] = _stamp()
    return GraphNode(
        id=node.id, type=node.type, name=node.name,
        service=node.service, attributes=attrs,
    )

def _apply_payload(
    graph,
    nodes: list[GraphNode],
    payload: dict[str, Any],
    fallback_summary: str,
    model: str,
    file_hash: str = "",
) -> int:
    written = 0
    by_id = {n.id: n for n in nodes}
    owners = _owner_nodes(nodes)
    summary = str(payload.get("class_summary") or fallback_summary).strip()
    spring_role = str(payload.get("spring_role") or "").strip()
    for owner in owners:
        graph.add_node(_merge_attrs(owner, {
            "llm_summary": summary,
            "llm_purpose": summary,
            "spring_role": spring_role,
            "llm_model": model,
            "llm_file_hash": file_hash,
        }))
        written += 1

    field_map = {str(item.get("name")): item for item in payload.get("fields") or [] if isinstance(item, dict)}
    ctor_map = {str(item.get("name")): item for item in payload.get("constructors") or [] if isinstance(item, dict)}
    method_map = {str(item.get("name")): item for item in payload.get("methods") or [] if isinstance(item, dict)}
    type_ann_map = {
        str(item.get("name")): item
        for item in payload.get("type_annotations") or []
        if isinstance(item, dict)
    }

    for node in nodes:
        if node.type == NodeType.FIELD:
            item = field_map.get(node.name) or {}
            purpose = str(item.get("purpose") or "").strip()
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
        elif node.type == NodeType.CONSTRUCTOR:
            item = ctor_map.get(node.name) or ctor_map.get(node.attributes.get("class", "")) or {}
            purpose = str(item.get("purpose") or "").strip()
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
        elif node.type == NodeType.METHOD:
            item = method_map.get(node.name) or {}
            purpose = str(item.get("purpose") or "").strip()
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
        elif node.type == NodeType.PARAMETER:
            method_name = str(node.attributes.get("method") or "")
            item = method_map.get(method_name) or {}
            param_map = {
                str(p.get("name")): p
                for p in item.get("parameters") or []
                if isinstance(p, dict)
            }
            purpose = str((param_map.get(node.name) or {}).get("purpose") or "").strip()
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
        elif node.type == NodeType.LOGICAL_BLOCK:
            method_name = str(node.attributes.get("method") or "")
            item = method_map.get(method_name) or {}
            start = node.attributes.get("start_line")
            kind = node.name
            purpose = ""
            for block in item.get("logic_blocks") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("kind") == kind and (
                    block.get("start_line") == start or block.get("start_line") is None
                ):
                    purpose = str(block.get("purpose") or "").strip()
                    break
            if not purpose:
                for block in item.get("logic_blocks") or []:
                    if isinstance(block, dict) and block.get("kind") == kind:
                        purpose = str(block.get("purpose") or "").strip()
                        if purpose:
                            break
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
        elif node.type == NodeType.ANNOTATION_USAGE:
            site = str(node.attributes.get("site") or "")
            meaning = str((type_ann_map.get(node.name) or {}).get("meaning") or "").strip()
            if not meaning:
                owner_method = str(node.attributes.get("owner") or "").rsplit(".", 1)[-1]
                method_item = method_map.get(owner_method) or {}
                for ann in method_item.get("annotations") or []:
                    if isinstance(ann, dict) and ann.get("name") == node.name:
                        meaning = str(ann.get("meaning") or "").strip()
                        break
            if meaning:
                graph.add_node(_merge_attrs(node, {
                    "llm_summary": meaning, "llm_purpose": meaning, "llm_model": model,
                    "site": site,
                }))
                written += 1
        elif node.type == NodeType.ENDPOINT:
            handler = str(node.attributes.get("handler") or node.name)
            item = method_map.get(handler) or {}
            purpose = str(item.get("purpose") or summary).strip()
            if purpose:
                graph.add_node(_merge_attrs(node, {"llm_summary": purpose, "llm_purpose": purpose, "llm_model": model}))
                written += 1
    _ = by_id
    return written


def _file_hash(java_path: Path, model: str) -> str:
    """Stable key that changes when the source file or model changes."""
    content = java_path.read_bytes()
    return hashlib.sha256(content + model.encode()).hexdigest()[:16]


def _already_enriched(graph, owner: GraphNode, file_hash: str | None = None) -> bool:
    existing = graph.node(owner.id)
    if not existing:
        return False
    attrs = existing.get("attributes") or {}
    if not (attrs.get("llm_enriched") and str(attrs.get("llm_summary") or "").strip()):
        return False
    # re-enrich if the source file changed or the model changed
    if file_hash and attrs.get("llm_file_hash") != file_hash:
        return False
    return True


def _method_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    return [n for n in nodes if n.type in (NodeType.METHOD, NodeType.CONSTRUCTOR)]


def _outline_batch(nodes: list[GraphNode], methods: list[GraphNode]) -> str:
    """Outline using non-method structure once, then only the given method slice."""
    lines: list[str] = []
    for n in nodes:
        if n.type == NodeType.FIELD:
            lines.append(f"- field {n.attributes.get('java_type', '?')} {n.name} anns={n.attributes.get('annotations')}")
        elif n.type == NodeType.ANNOTATION_USAGE:
            lines.append(f"- @{n.name} on {n.attributes.get('owner')} ({n.attributes.get('site')})")
    for n in methods:
        params = n.attributes.get("parameters") or []
        ptxt = ", ".join(f"{p.get('type')} {p.get('name')}" for p in params if isinstance(p, dict))
        lines.append(f"- {n.type.value} {n.name}({ptxt}) anns={n.attributes.get('annotations')}")
        for bn in nodes:
            if bn.type == NodeType.LOGICAL_BLOCK and bn.attributes.get("method") == n.name:
                lines.append(
                    f"  - block {bn.name} L{bn.attributes.get('start_line')}: "
                    f"{str(bn.attributes.get('condition') or bn.attributes.get('excerpt') or '')[:80]}"
                )
    return "\n".join(lines[:120])


def _outline(nodes: list[GraphNode]) -> str:
    return _outline_batch(nodes, _method_nodes(nodes))


def _build_prompts(
    *,
    repo: str,
    rel_path: str,
    owners: list[GraphNode],
    nodes: list[GraphNode],
    src_text: str,
) -> list[str]:
    """Return one or more prompts; batches methods when the file is large."""
    methods = _method_nodes(nodes)
    type_names = ', '.join(sorted({o.name for o in owners}))
    # single pass for small files
    if len(methods) <= _METHOD_BATCH_SIZE:
        prompt = (
            f"{_PROMPT_PREAMBLE}\nService: {repo}\nFile: {rel_path}\n"
            f"Types: {type_names}\n"
            f"Structure:\n{_outline(nodes)}\n\n"
            f"Source:\n```java\n{src_text}\n```\n"
        )
        return [prompt]

    # first pass: class-level summary + fields only, no methods
    class_prompt = (
        f"{_PROMPT_PREAMBLE}\nService: {repo}\nFile: {rel_path}\n"
        f"Types: {type_names}\n"
        "Task: document only the class-level summary, fields, and type annotations. "
        "Return empty arrays for constructors and methods.\n"
        f"Source (first 4000 chars):\n```java\n{src_text[:4000]}\n```\n"
    )
    prompts = [class_prompt]

    # method batches — include source lines around those methods if available
    for i in range(0, len(methods), _METHOD_BATCH_SIZE):
        batch = methods[i: i + _METHOD_BATCH_SIZE]
        batch_outline = _outline_batch(nodes, batch)
        names = ", ".join(n.name for n in batch)
        batch_prompt = (
            f"{_PROMPT_PREAMBLE}\nService: {repo}\nFile: {rel_path}\n"
            f"Types: {type_names}\n"
            f"Task: document ONLY these methods (leave class_summary, fields, "
            f"type_annotations as empty strings/arrays): {names}\n"
            f"Structure:\n{batch_outline}\n\n"
            f"Source:\n```java\n{src_text}\n```\n"
        )
        prompts.append(batch_prompt)
    return prompts


def _merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-batch LLM responses into one unified payload."""
    merged: dict[str, Any] = {}
    all_methods: list[dict] = []
    for p in payloads:
        if not merged.get("class_summary") and p.get("class_summary"):
            merged["class_summary"] = p["class_summary"]
        if not merged.get("spring_role") and p.get("spring_role"):
            merged["spring_role"] = p["spring_role"]
        if not merged.get("fields") and p.get("fields"):
            merged["fields"] = p["fields"]
        if not merged.get("constructors") and p.get("constructors"):
            merged["constructors"] = p["constructors"]
        if not merged.get("type_annotations") and p.get("type_annotations"):
            merged["type_annotations"] = p["type_annotations"]
        all_methods.extend(p.get("methods") or [])
    merged["methods"] = all_methods
    return merged


def _prepare_file(
    *,
    parser: JavaParser,
    graph,
    repo: str,
    repo_dir: Path,
    java_path: Path,
    graph_lock: threading.Lock,
    model: str,
    file_index: int = 0,
    total_files: int = 0,
) -> tuple[str, list[GraphNode], list[str], str] | None:
    """Parse + persist structure, returning (rel_path, nodes, prompts, file_hash) or None to skip."""
    fhash = _file_hash(java_path, model)
    source = _source_file(repo, repo_dir, java_path)
    with graph_lock:
        parsed = parser.parse(source)
        owners = _owner_nodes(parsed.nodes)
        if not owners:
            return None
        # Check enrichment BEFORE add_many; add_many overwrites attributes_json,
        # wiping llm_file_hash/llm_enriched stored from a previous run.
        if all(_already_enriched(graph, owner, fhash) for owner in owners):
            progress = f"{file_index}/{total_files}" if total_files else "-"
            # print(f"[skip {progress}] {repo}/{source.rel_path}")
            log.info("llm_enrich_skip_unchanged", progress=progress, repo=repo, file=source.rel_path)
            return None
        if parsed.nodes:
            # Remove stale nodes from previous parse before writing fresh ones
            graph.remove_service_file(repo, source.rel_path)
            graph.add_many(parsed.nodes, parsed.edges)

    src_text = java_path.read_text(encoding="utf-8", errors="replace")[:12000]
    prompts = _build_prompts(
        repo=repo, rel_path=source.rel_path, owners=owners,
        nodes=parsed.nodes, src_text=src_text,
    )
    return source.rel_path, parsed.nodes, prompts, fhash


def enrich_java_file(
    *,
    parser: JavaParser,
    graph,
    settings: Settings,
    repo: str,
    repo_dir: Path,
    java_path: Path,
    model: str,
    graph_lock: threading.Lock | None = None,
    file_index: int = 0,
    total_files: int = 0,
) -> int:
    """Enrich one Java file's parsed graph nodes via Ollama and write the results back to the graph."""
    lock = graph_lock or threading.Lock()
    prepared = _prepare_file(
        parser=parser, graph=graph, repo=repo,
        repo_dir=repo_dir, java_path=java_path, graph_lock=lock, model=model,
        file_index=file_index, total_files=total_files,
    )
    if prepared is None:
        return 0
    rel_path, nodes, prompts, fhash = prepared

    t0 = time.time()
    payloads: list[dict[str, Any]] = []
    for i, prompt in enumerate(prompts):
        raw = ollama_generate(prompt, settings)
        p = _parse_json_object(raw)
        if not p and i == 0 and len(prompts) == 1:
            # single-pass failed; store raw excerpt so the node is still marked done
            p = {"class_summary": raw[:400]}
        if p:
            payloads.append(p)
        else:
            log.warning("llm_enrich_batch_empty", repo=repo, file=rel_path, batch=i)

    payload = _merge_payloads(payloads) if payloads else {}
    with lock:
        written = _apply_payload(graph, nodes, payload, "", model, fhash)
    progress = f"{file_index}/{total_files}" if total_files else "-"
    log.info(
        "llm_enrich_file",
        progress=progress,
        repo=repo,
        file=rel_path,
        nodes=written,
        batches=len(prompts),
        seconds=round(time.time() - t0, 1),
        parsed_json=bool(payload),
    )
    # print(f"[{progress}] {repo}/{rel_path}  nodes={written} batches={len(prompts)} {round(time.time() - t0, 1)}s")
    return written


def run_enrichment(
    *, repo: str | None = None, skip_sync: bool = False, workers: int | None = None,
) -> int:
    """Enrich every (or one) repo's Java graph nodes via Ollama, then sync to Qdrant."""
    settings = get_settings()
    if workers is None:
        workers = settings.llm_max_concurrency
    graph = get_graph_store(settings)
    graph.load()
    parser = JavaParser(settings)
    _, model = _ollama_config(settings)
    log.info("llm_enrichment_start", model=model, workers=workers)

    repos_root = Path(settings.repos_root)
    repo_dirs = sorted(p for p in repos_root.iterdir() if p.is_dir())
    if repo:
        repo_dirs = [p for p in repo_dirs if p.name == repo]
        if not repo_dirs:
            raise SystemExit(f"Unknown repo {repo!r} under {repos_root}")

    graph_lock = threading.Lock()
    total = 0
    for repo_dir in repo_dirs:
        java_files = _iter_java_files(repo_dir)
        log.info("llm_enrich_repo", repo=repo_dir.name, java_files=len(java_files))
        total_files = len(java_files)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(
                    enrich_java_file,
                    parser=parser,
                    graph=graph,
                    settings=settings,
                    repo=repo_dir.name,
                    repo_dir=repo_dir,
                    java_path=java_path,
                    model=model,
                    graph_lock=graph_lock,
                    file_index=idx + 1,
                    total_files=total_files,
                ): java_path
                for idx, java_path in enumerate(java_files)
            }
            for future in as_completed(futures):
                try:
                    total += future.result()
                except Exception as exc:
                    log.warning(
                        "llm_enrich_file_failed",
                        repo=repo_dir.name, file=str(futures[future]), error=str(exc),
                    )
    graph.save()
    synced = 0 if skip_sync else sync_ollama_enrichment(settings)
    log.info("llm_enrichment_done", neo4j_updates=total, qdrant_chunks=synced)
    return total


def cli_main() -> None:
    """CLI entrypoint: enrich Java/Spring graph nodes with Ollama and persist to Neo4j + Qdrant."""
    parser = argparse.ArgumentParser(
    )
    parser.add_argument("--repo", help="limit to one repository name")
    parser.add_argument("--skip-sync", action="store_true", help="do not write Qdrant semantic chunks")
    parser.add_argument(
        "--workers", type=int, default=None,
        help="concurrent LLM requests; defaults to LLM_MAX_CONCURRENCY from .env",
    )
    args = parser.parse_args()
    run_enrichment(repo=args.repo, skip_sync=args.skip_sync, workers=args.workers)


if __name__ == "__main__":
    cli_main()
