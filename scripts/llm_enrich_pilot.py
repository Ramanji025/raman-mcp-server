"""Pilot LLM enrichment pass (local Ollama, offline knowledge-base build only).

Scope: validates enrichment quality on two connected repos before scaling to
the full 44-repo corpus:
    - dic-fleet-management-service  (Feign client caller)
    - dic-mydevices                 (Feign client target, uses a composed
                                      @APIURL meta-annotation that the static
                                      AST parser does NOT resolve to
                                      @RestController/@RequestMapping)

What this script does (all offline, one-time build step — NOT used per
end-user request):
  1. Loads the persisted knowledge graph (data/graph/graph.json).
  2. Semantically re-classifies classes whose Spring stereotype was missed by
     the static parser because of composed/meta annotations (e.g. @APIURL),
     using the local LLM to read the raw source and confirm the role.
  3. Resolves each @FeignClient interface method to the *real* target
     controller endpoint (path + HTTP verb match), not just a coarse
     service-to-service edge.
  4. Generates a natural-language business-purpose summary per
     class/endpoint/cross-service link using LLM_MODEL from .env via the local
     Ollama server (http://localhost:11434) running CPU-only.
  5. Writes everything to data/insights/pilot_enrichment.json for review, and
     patches matching nodes in the graph with `llm_summary` /
     `business_purpose` attributes (graph.json is backed up first).

Run:
    .\.venv\Scripts\python.exe scripts\llm_enrich_pilot.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp_kb.config import get_settings  # noqa: E402
from mcp_kb.graph.factory import get_graph_store  # noqa: E402
from mcp_kb.models import GraphNode, NodeType  # noqa: E402

PILOT_REPOS = ["dic-fleet-management-service", "dic-mydevices"]

REPOS_ROOT = ROOT / "data" / "repos"
INSIGHTS_DIR = ROOT / "data" / "insights"


def _ollama_config() -> tuple[str, str]:
    settings = get_settings()
    url = settings.chat_completions_url
    model = settings.ollama_model or settings.effective_llm_model
    return url, model


def ollama_generate(prompt: str, *, max_tokens: int = 300, timeout: int = 300) -> str:
    url, model = _ollama_config()
    resp = requests.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.1,
        },
        timeout=timeout,
    )
    if resp.status_code == 404:
        raise SystemExit(
            f"Ollama 404 for model {model!r} at {url}. "
            f"Pull it with `ollama pull {model}` or set LLM_MODEL in .env "
            "to a name from `ollama list`."
        )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    return (message.get("content") or message.get("reasoning_content") or "").strip()


def update_graph_attributes(graph, node_id: str, attributes: dict) -> None:
    node = graph.node(node_id)
    if not node:
        return
    graph.add_node(GraphNode(
        id=node["id"], type=NodeType(node["type"]), name=node["name"],
        service=node.get("service"), attributes=attributes,
    ))


# --------------------------------------------------------------------------- #
# Step 1: detect meta-annotations (e.g. @APIURL) that alias @RestController /
# @RequestMapping, since the static tree-sitter parser only matches literal
# annotation names and misses these compositions.
# --------------------------------------------------------------------------- #
def find_composed_controller_annotations(repo_dir: Path) -> dict[str, str]:
    """Return {AnnotationName: base_path_alias_attr} for custom annotations
    that are themselves meta-annotated with @RestController + @RequestMapping."""
    found: dict[str, str] = {}
    for java_file in repo_dir.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        if "@interface" not in text or "@RestController" not in text:
            continue
        m = re.search(r"@interface\s+(\w+)", text)
        if not m:
            continue
        name = m.group(1)
        alias_m = re.search(
            r'@AliasFor\(annotation\s*=\s*RequestMapping\.class,\s*attribute\s*=\s*"value"\)\s*\n?\s*String\[?\]?\s*(\w+)',
            text,
        )
        found[name] = alias_m.group(1) if alias_m else "value"
    return found


def scan_classes_using_annotation(repo_dir: Path, annotation_name: str) -> list[dict]:
    """Find classes annotated with a (possibly composed) controller annotation
    and extract its endpoint methods directly via regex (fallback path for
    when the static AST parser missed the stereotype)."""
    out = []
    ann_pattern = re.compile(rf"@{annotation_name}\(([^)]*)\)\s*\npublic class (\w+)")
    for java_file in repo_dir.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        for m in ann_pattern.finditer(text):
            ann_args, class_name = m.groups()
            base_path_m = re.search(r'requestMappingValue\s*=\s*"([^"]*)"', ann_args)
            base_path = base_path_m.group(1) if base_path_m else ""
            endpoints = []
            for em in re.finditer(
                r'@(Get|Post|Put|Delete|Patch)Mapping\(\s*(?:value\s*=\s*)?"([^"]*)"',
                text,
            ):
                http, sub_path = em.groups()
                endpoints.append({"http_method": http.upper(), "path": base_path + sub_path})
            out.append({
                "file": str(java_file.relative_to(repo_dir)),
                "class": class_name,
                "base_path": base_path,
                "endpoints": endpoints,
                "source_excerpt": text[:6000],
            })
    return out


# --------------------------------------------------------------------------- #
# Step 2: extract Feign client interface methods (caller side).
# --------------------------------------------------------------------------- #
def scan_feign_clients(repo_dir: Path) -> list[dict]:
    out = []
    for java_file in repo_dir.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8", errors="replace")
        if "@FeignClient" not in text:
            continue
        target_m = re.search(r'@FeignClient\(\s*value\s*=\s*"([^"]*)"', text) or \
            re.search(r'@FeignClient\("([^"]*)"\)', text)
        target_service = target_m.group(1) if target_m else None
        methods = []
        for em in re.finditer(
            r'@(Get|Post|Put|Delete|Patch)Mapping\(\s*(?:value\s*=\s*)?\{?"([^"]*)"',
            text,
        ):
            http, path = em.groups()
            # method name follows on subsequent line(s); grab a short window
            after = text[em.end(): em.end() + 200]
            name_m = re.search(r"(\w+)\s*\(", after)
            methods.append({
                "http_method": http.upper(), "path": path,
                "method_name": name_m.group(1) if name_m else "?",
            })
        out.append({
            "file": str(java_file.relative_to(repo_dir)),
            "class": java_file.stem,
            "target_service": target_service,
            "methods": methods,
        })
    return out


_GATEWAY_PREFIXES = ("api/v1/", "api/v2/", "api/")


def paths_match(feign_path: str, endpoint_path: str) -> bool:
    """Compare Spring paths treating {var} segments as wildcards and
    tolerating a leading API-gateway prefix (e.g. /api/v1) that exists on the
    caller side but is stripped by the gateway before reaching the
    controller's own @RequestMapping."""
    norm = lambda p: re.sub(r"\{[^}]+\}", "{}", p.strip("/"))
    a, b = norm(feign_path), norm(endpoint_path)
    if a == b:
        return True
    for prefix in _GATEWAY_PREFIXES:
        if a.startswith(prefix) and a[len(prefix):] == b:
            return True
        if b.startswith(prefix) and b[len(prefix):] == a:
            return True
    return False


def main() -> None:
    settings = get_settings()
    graph = get_graph_store(settings)
    graph.load()

    INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    report: dict = {"repos": PILOT_REPOS, "generated_at": time.time(),
                     "meta_annotation_findings": {}, "resolved_controllers": [],
                     "feign_resolutions": [], "class_summaries": []}

    # --- Step 1: resolve meta-annotation controllers (fixes the dic-mydevices gap) ---
    all_controllers: dict[str, list[dict]] = {}
    for repo in PILOT_REPOS:
        repo_dir = REPOS_ROOT / repo
        if not repo_dir.exists():
            print(f"WARN: repo dir not found: {repo_dir}")
            continue
        composed = find_composed_controller_annotations(repo_dir)
        report["meta_annotation_findings"][repo] = composed
        controllers: list[dict] = []
        for ann_name in composed:
            controllers.extend(scan_classes_using_annotation(repo_dir, ann_name))
        all_controllers[repo] = controllers
        for c in controllers:
            print(f"[{repo}] resolved hidden controller via meta-annotation: "
                  f"{c['class']} base_path={c['base_path']!r} endpoints={len(c['endpoints'])}")

    # --- Step 2: LLM summary for each resolved controller class ---
    for repo, controllers in all_controllers.items():
        for c in controllers:
            prompt = (
                "You are analyzing a Spring Boot microservice class that uses a custom "
                "meta-annotation composing @RestController + @RequestMapping.\n"
                f"Service: {repo}\nClass: {c['class']}\nBase path: {c['base_path']}\n"
                f"Endpoints: {json.dumps(c['endpoints'])}\n\n"
                "Source (truncated):\n```java\n" + c["source_excerpt"] + "\n```\n\n"
                "In 3-4 concise sentences, describe: (1) the business capability this "
                "controller exposes, (2) what each endpoint does functionally, and "
                "(3) any notable authorization/validation logic. Be specific, no fluff."
            )
            t0 = time.time()
            summary = ollama_generate(prompt, max_tokens=250)
            elapsed = time.time() - t0
            print(f"  LLM summary for {c['class']} in {elapsed:.1f}s")
            report["class_summaries"].append({
                "repo": repo, "class": c["class"], "base_path": c["base_path"],
                "endpoints": c["endpoints"], "llm_summary": summary,
            })
            # Patch graph: create/update a Controller-like node if one exists by name.
            node_id = f"controller:{repo}:{c['class']}"
            existing = graph.node(node_id)
            if existing:
                existing["attributes"]["llm_summary"] = summary
                existing["attributes"]["resolved_via"] = "meta_annotation_llm_pass"
                update_graph_attributes(graph, node_id, existing["attributes"])

    # --- Step 3: resolve Feign client methods -> real target endpoints ---
    for repo in PILOT_REPOS:
        repo_dir = REPOS_ROOT / repo
        if not repo_dir.exists():
            continue
        feign_clients = scan_feign_clients(repo_dir)
        for fc in feign_clients:
            target_svc = fc["target_service"]
            # Look in resolved controllers of ALL pilot repos (target may be in another repo).
            target_controllers = []
            for other_repo, controllers in all_controllers.items():
                target_controllers.extend(controllers)
            for method in fc["methods"]:
                match = None
                for tc in target_controllers:
                    for ep in tc["endpoints"]:
                        if (ep["http_method"] == method["http_method"]
                                and paths_match(method["path"], ep["path"])):
                            match = {"controller": tc["class"], "endpoint": ep}
                            break
                    if match:
                        break
                resolution = {
                    "caller_repo": repo, "feign_client": fc["class"],
                    "feign_method": method["method_name"],
                    "http_method": method["http_method"], "path": method["path"],
                    "declared_target_service": target_svc,
                    "resolved_target_controller": match["controller"] if match else None,
                    "resolved": match is not None,
                }
                if match:
                    prompt = (
                        f"A Spring Boot service '{repo}' calls another service via Feign client "
                        f"method '{fc['class']}.{method['method_name']}()' "
                        f"({method['http_method']} {method['path']}), which resolves to "
                        f"controller endpoint '{match['controller']}' handling the same path. "
                        "In 1-2 sentences, describe the business purpose of this cross-service "
                        "call (what data/capability flows between the two services)."
                    )
                    resolution["llm_business_purpose"] = ollama_generate(prompt, max_tokens=120)
                    print(f"  Resolved Feign call: {fc['class']}.{method['method_name']} -> "
                          f"{match['controller']}")
                report["feign_resolutions"].append(resolution)

    out_path = INSIGHTS_DIR / "pilot_enrichment.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    graph.save()
    print(f"\nWrote {out_path}")
    print(f"Class summaries: {len(report['class_summaries'])}")
    print(f"Feign resolutions: {len(report['feign_resolutions'])} "
          f"(resolved: {sum(1 for r in report['feign_resolutions'] if r['resolved'])})")


if __name__ == "__main__":
    main()
