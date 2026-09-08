"""Phase 4.1: generic multi-language tree-sitter extraction engine.

Single walker driven entirely by a per-language `LangSpec` (see
`langspec.py`) — mirrors codebase-memory-mcp's table-driven design so a new
language is one spec-table entry, not a new visitor class. Produces the same
NodeType/EdgeType schema the Java parser writes (Module/Class/Interface/
Method + DECLARED_IN/CALLS edges + body_tokens for the Phase 2 similarity
pass), so every existing graph tool works unchanged for any language wired
in here.

Call resolution is intentionally a single, precision-first tier (same-owner
exact match, then whole-file unique-suffix match) — it does not attempt
Java's Spring-DI-aware field-injection resolution (that remains a
Java/Spring-specific enrichment in graph/enterprise_dependency_graph.py),
since no other language here has an equivalent injection-container
convention to reason about.
"""
from __future__ import annotations

import functools
import re
from collections.abc import Iterable

from tree_sitter import Language, Node
from tree_sitter import Parser as TSParser
from tree_sitter_language_pack import get_language

from ...models import Chunk, EdgeType, GraphEdge, GraphNode, NodeType, ParseResult, SourceFile
from ..langspec import LangSpec
from .base import Parser, make_chunk_id, make_node_id

_BODY_CAP = 4000
_CHUNK_CAP = 8000
_BODY_TOKENS_MAX = 128
_MIN_IDENT_LEN = 3
_MAX_IMPORTS = 50


@functools.lru_cache(maxsize=None)
def _get_ts_language(name: str) -> Language:
    """Compile/cache one tree-sitter Language object per grammar name (process-wide)."""
    return get_language(name)  # type: ignore[arg-type]


class GenericTreeSitterParser(Parser):
    """Table-driven extractor: one instance per `LangSpec` (one per language)."""

    def __init__(self, settings, spec: LangSpec) -> None:
        super().__init__(settings)
        self.spec = spec
        self._parser = TSParser(_get_ts_language(spec.ts_language))

    def parse(self, source: SourceFile) -> ParseResult:
        result = ParseResult()
        service = self._service_name(source)
        try:
            with open(source.abs_path, "rb") as fh:
                src = fh.read()
        except OSError:
            return result

        spec = self.spec
        root = self._parser.parse(src).root_node

        # Same id convention as git_coupling.py's File nodes, so both passes
        # (co-change coupling + static extraction) agree on one node per file.
        module_id = make_node_id("file", service, source.rel_path)
        imports = self._extract_imports(root, src)
        result.nodes.append(GraphNode(
            id=module_id, type=NodeType.FILE, name=source.rel_path, service=service,
            attributes={"language": spec.key, "file": source.rel_path, "imports": imports},
        ))

        # Pass 1: class-like containers, so methods can resolve their owner name.
        class_name_by_ts_id: dict[int, str] = {}
        for node in self._iter_descendants(root, spec.class_types):
            name = self._extract_name(node, spec.name_field, src) or \
                (self._extract_name(node, spec.name_field_fallback, src) if spec.name_field_fallback else None) or \
                "Anonymous"
            class_name_by_ts_id[node.id] = name
            bases = self._field_text(node, spec.bases_field, src) if spec.bases_field else None
            class_id = make_node_id("class", service, name)
            node_type = NodeType.INTERFACE if "interface" in node.type else NodeType.CLASS
            result.nodes.append(GraphNode(
                id=class_id, type=node_type, name=name, service=service,
                attributes={"language": spec.key, "file": source.rel_path, "bases": bases or "",
                           "start_line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1},
            ))
            result.edges.append(GraphEdge(src=module_id, dst=class_id, type=EdgeType.DECLARED_IN))

        # Pass 2: functions/methods (both map to NodeType.METHOD — schema
        # normalization: every language-specific "callable" is one node type).
        method_registry: dict[tuple[str, str], str] = {}      # (owner, name) -> method_id
        method_params: dict[str, list[tuple[str, str]]] = {}   # method_id -> [(param_name, param_node_id)]
        pending_calls: list[tuple[str, str, list[dict]]] = []  # (method_id, owner, calls)
        for node in self._iter_descendants(root, spec.function_types):
            name = self._extract_name(node, spec.name_field, src) or "anonymous"
            owner = self._owner_name(node, class_name_by_ts_id, src)
            qualified = f"{owner}.{name}" if owner else name
            method_id = make_node_id("method", service, qualified)
            method_registry[(owner, name)] = method_id

            body_node = node.child_by_field_name(spec.body_field)
            body_text = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
            body_tokens = self._body_tokens(body_node, src) if body_node is not None else ""
            calls = self._extract_calls(body_node, src) if body_node is not None else []
            params_text = self._field_text(node, spec.params_field, src) or ""
            param_names = self._extract_param_names(params_text)
            returns = (self._field_text(node, spec.return_type_field, src)
                      if spec.return_type_field else None)
            decorators = self._collect_decorators(node, src)

            result.nodes.append(GraphNode(
                id=method_id, type=NodeType.METHOD, name=name, service=service,
                attributes={
                    "language": spec.key, "class": owner, "qualified_name": qualified,
                    "parameters_text": params_text, "parameter_names": param_names,
                    "returns": returns or "",
                    "decorators": decorators,
                    "called_methods": [f"{c['receiver']}.{c['name']}" if c["receiver"] else c["name"]
                                       for c in calls][:30],
                    "body": body_text[:_BODY_CAP], "body_tokens": body_tokens,
                    "start_line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1,
                    "file": source.rel_path,
                },
            ))
            owner_id = make_node_id("class", service, owner) if owner else module_id
            result.edges.append(GraphEdge(src=owner_id, dst=method_id, type=EdgeType.DECLARED_IN))

            # Parameter nodes (Phase 6.x: DATA_FLOWS arg→param binding needs a
            # concrete node per parameter, not just the raw text).
            param_entries: list[tuple[str, str]] = []
            for idx, pname in enumerate(param_names):
                param_id = make_node_id("parameter", service, f"{qualified}#{idx}:{pname}")
                result.nodes.append(GraphNode(
                    id=param_id, type=NodeType.PARAMETER, name=pname, service=service,
                    attributes={"language": spec.key, "method": qualified, "position": idx},
                ))
                result.edges.append(GraphEdge(src=method_id, dst=param_id, type=EdgeType.HAS_PARAMETER))
                param_entries.append((pname, param_id))
            method_params[method_id] = param_entries
            pending_calls.append((method_id, owner, calls))

            result.chunks.append(Chunk(
                id=make_chunk_id(source.repo, source.rel_path, node.start_byte, qualified),
                repo=source.repo, rel_path=source.rel_path, content_type=source.content_type,
                collection="code", text=body_text[:_CHUNK_CAP],
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                metadata={"kind": "method", "language": spec.key, "qualified_name": qualified,
                         "service": service},
            ))

        # Pass 3: same-file call resolution — exact same-owner match first,
        # then a whole-file unique-suffix match (skipped if ambiguous, to
        # avoid emitting a wrong edge — precision over recall).
        by_name: dict[str, list[tuple[str, str]]] = {}
        for (owner, name), method_id in method_registry.items():
            by_name.setdefault(name, []).append((owner, method_id))

        for method_id, owner, calls in pending_calls:
            caller_params = dict(method_params.get(method_id, []))
            for call in calls:
                receiver = call["receiver"]
                name = call["name"]
                target_id: str | None = None
                if receiver in (None, "self", "this", "cls"):
                    target_id = method_registry.get((owner, name)) or method_registry.get(("", name))
                else:
                    candidates = by_name.get(name, [])
                    if len(candidates) == 1:
                        target_id = candidates[0][1]
                if target_id and target_id != method_id:
                    result.edges.append(GraphEdge(
                        src=method_id, dst=target_id, type=EdgeType.CALLS,
                        attributes={"via": "generic_call", "language": spec.key},
                    ))
                    # DATA_FLOWS: parameter-passthrough only (precision-first —
                    # an argument that's a bare identifier matching one of the
                    # *caller's own* parameter names, positionally bound to the
                    # callee's parameter). Local-variable dataflow (assignments,
                    # field reads) is intentionally out of scope here.
                    target_params = method_params.get(target_id, [])
                    # Instance-method call sites (`obj.method(x)`) never pass
                    # self/this/cls explicitly even when it IS a declared
                    # parameter (Python/Rust/Ruby-style) — skip it so position
                    # 0 of the call's args aligns with position 1 of the
                    # declared parameters, not position 0.
                    offset = (1 if receiver is not None and target_params
                             and target_params[0][0] in ("self", "this", "cls") else 0)
                    for pos, arg_name in enumerate(call.get("args") or []):
                        target_pos = pos + offset
                        if target_pos >= len(target_params) or arg_name not in caller_params:
                            continue
                        result.edges.append(GraphEdge(
                            src=caller_params[arg_name], dst=target_params[target_pos][1],
                            type=EdgeType.DATA_FLOWS,
                            attributes={"via": "generic_arg_passthrough", "language": spec.key},
                        ))
        return result

    # ------------------------------------------------------------------ #
    # Generic tree-sitter helpers (spec-table driven, no per-language code)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _text(node: Node | None, src: bytes) -> str:
        if node is None:
            return ""
        return src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def _field_text(self, node: Node, field_name: str | None, src: bytes) -> str | None:
        if not field_name:
            return None
        child = node.child_by_field_name(field_name)
        return self._text(child, src) if child is not None else None

    _PARAM_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    @staticmethod
    def _split_top_level(text: str) -> list[str]:
        """Split on commas that aren't nested inside (), [], {}, or <>."""
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        for ch in text:
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append("".join(current))
        return [p.strip() for p in parts if p.strip()]

    def _extract_param_names(self, params_text: str) -> list[str]:
        """Best-effort parameter-name extraction from raw parameter-list text,
        per the language's `param_name_style` (see langspec.py). Not a full
        parser — good enough for DATA_FLOWS arg-passthrough binding, not for
        anything requiring exact type information."""
        style = self.spec.param_name_style
        text = params_text.strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        names: list[str] = []
        for frag in self._split_top_level(text):
            frag = frag.split("=", 1)[0].strip()  # drop default value
            if not frag:
                continue
            name: str | None = None
            if style == "colon":
                head = frag.split(":", 1)[0].strip().lstrip("&*")
                m = self._PARAM_IDENT_RE.search(head) if head else None
                name = m.group(0) if m else None
            elif style == "first":
                m = self._PARAM_IDENT_RE.match(frag.lstrip("*"))
                name = m.group(0) if m else None
            elif style == "dollar":
                m = re.search(r"\$([A-Za-z_][A-Za-z0-9_]*)", frag)
                name = m.group(1) if m else None
            elif style == "bare":
                m = self._PARAM_IDENT_RE.match(frag)
                name = m.group(0) if m else None
            else:  # "last" (C/C++/C#-style: "Type name")
                matches = self._PARAM_IDENT_RE.findall(frag)
                name = matches[-1] if matches else None
            if name:
                names.append(name)
        return names

    @staticmethod
    def _iter_descendants(node: Node, types: frozenset[str]) -> Iterable[Node]:
        """Stack-based DFS yielding every *named* descendant whose type is in
        `types`. Anonymous/keyword tokens are excluded — some grammars reuse
        a construct's own keyword as its literal node type (e.g. Ruby's
        `class` keyword token has type "class", same as the class_definition
        node itself), which would otherwise falsely match a class/function
        node-type set."""
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.is_named and n.type in types:
                yield n
            stack.extend(n.children)

    # Node types that are themselves a usable "name" leaf (identifier-shaped).
    _LEAF_NAME_TYPES = frozenset({
        "identifier", "field_identifier", "type_identifier", "property_identifier",
        "simple_identifier", "name", "constant", "word", "command_name",
    })

    def _extract_name(self, node: Node, field_name: str | None, src: bytes) -> str | None:
        """Return the leaf identifier text at `field_name`, descending through
        nested declarators if needed (e.g. C/C++ `function_definition.declarator`
        is a `function_declarator` wrapping the real `identifier` one level
        deeper — recursing via the same field name unwraps any pointer/array
        declarator layers generically, without a per-language special case)."""
        if not field_name:
            return None
        child = node.child_by_field_name(field_name)
        depth = 0
        while child is not None and child.type not in self._LEAF_NAME_TYPES and depth < 6:
            nxt = child.child_by_field_name(field_name) or child.child_by_field_name("declarator")
            if nxt is None:
                break
            child = nxt
            depth += 1
        return self._text(child, src) if child is not None else None

    def _owner_name(self, node: Node, class_name_by_ts_id: dict[int, str], src: bytes) -> str:
        """Resolve the owning class/type name for a function/method node."""
        spec = self.spec
        if spec.receiver_field:
            receiver = node.child_by_field_name(spec.receiver_field)
            if receiver is not None:
                # Go: `func (f *Foo) Bar()` — receiver is a parameter_list
                # containing one parameter_declaration whose type may be a
                # pointer_type wrapping a type_identifier.
                type_text = self._text(receiver, src)
                type_text = type_text.strip("()").split()[-1] if type_text.strip("()") else ""
                return type_text.lstrip("*").strip()
        parent = node.parent
        while parent is not None:
            if parent.id in class_name_by_ts_id:
                return class_name_by_ts_id[parent.id]
            parent = parent.parent
        return ""

    def _body_tokens(self, body_node: Node, src: bytes) -> str:
        """AST-derived identifier shingle set for MinHash similarity (Phase 2)."""
        seen: list[str] = []
        seen_set: set[str] = set()
        for node in self._iter_descendants(body_node, self.spec.identifier_types):
            token = self._text(node, src).strip().lower()
            if len(token) < _MIN_IDENT_LEN or token in seen_set:
                continue
            seen_set.add(token)
            seen.append(token)
            if len(seen) >= _BODY_TOKENS_MAX:
                break
        return " ".join(seen)

    def _extract_calls(self, body_node: Node, src: bytes) -> list[dict]:
        """Return [{"receiver": str|None, "name": str, "args": [str]}] for every
        call in a body. `args` holds the bare-identifier text of each top-level
        call argument (non-identifier arguments become None placeholders, to
        keep positional alignment with the callee's parameters intact)."""
        spec = self.spec
        calls: list[dict] = []
        for call_node in self._iter_descendants(body_node, spec.call_types):
            args = self._extract_call_args(call_node, src)
            # Shape (b): receiver/name fields directly on the call node itself
            # (Ruby `call`, PHP `member_call_expression`) — no nested
            # member-access sub-expression to unwrap, unlike shape (a).
            if spec.call_receiver_field and spec.call_name_field:
                name_node = call_node.child_by_field_name(spec.call_name_field)
                if name_node is None:
                    continue
                receiver_node = call_node.child_by_field_name(spec.call_receiver_field)
                calls.append({"receiver": self._text(receiver_node, src) or None,
                             "name": self._text(name_node, src), "args": args})
                continue

            # Shape (a): a `function` sub-expression that's either a bare
            # identifier or a member-access node to unwrap (object.method()).
            fn_node = call_node.child_by_field_name(spec.call_function_field)
            if fn_node is None:
                continue
            if fn_node.type in spec.member_access_types:
                obj_node = fn_node.child_by_field_name(spec.member_object_field)
                name_node = fn_node.child_by_field_name(spec.member_name_field)
                if name_node is None:
                    continue
                calls.append({"receiver": self._text(obj_node, src) or None,
                             "name": self._text(name_node, src), "args": args})
            elif fn_node.type in spec.identifier_types:
                calls.append({"receiver": None, "name": self._text(fn_node, src), "args": args})
        return calls

    def _extract_call_args(self, call_node: Node, src: bytes) -> list[str | None]:
        """Best-effort positional argument list: bare-identifier text per
        top-level argument, or None for anything more complex (literals,
        nested expressions) — used only for DATA_FLOWS param-passthrough."""
        spec = self.spec
        if not spec.args_field:
            return []
        args_node = call_node.child_by_field_name(spec.args_field)
        if args_node is None:
            return []
        out: list[str | None] = []
        for child in args_node.named_children:
            out.append(self._text(child, src) if child.type in spec.identifier_types else None)
        return out

    def _collect_decorators(self, node: Node, src: bytes) -> list[str]:
        """Collect decorator/attribute text immediately preceding `node` among
        its siblings (covers both Python's `decorated_definition` wrapper and
        TS/JS's flat sibling-list class-body convention with one rule)."""
        spec = self.spec
        if not spec.decorator_types:
            return []
        parent = node.parent
        if parent is None:
            return []
        siblings = parent.children
        try:
            idx = siblings.index(node)
        except ValueError:
            return []
        decorators: list[str] = []
        for sib in reversed(siblings[:idx]):
            if sib.type in spec.decorator_types:
                decorators.append(self._text(sib, src).strip())
            elif sib.type != "comment":
                break
        decorators.reverse()
        return decorators

    def _extract_imports(self, root: Node, src: bytes) -> list[str]:
        seen: list[str] = []
        seen_set: set[str] = set()
        for node in self._iter_descendants(root, self.spec.import_types):
            text = self._text(node, src).strip().replace("\n", " ")[:200]
            if text and text not in seen_set:
                seen_set.add(text)
                seen.append(text)
            if len(seen) >= _MAX_IMPORTS:
                break
        return seen
