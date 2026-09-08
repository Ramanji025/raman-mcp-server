"""Phase 4.1: generic multi-language extraction spec table.

Mirrors codebase-memory-mcp's `CBMLangSpec` design: a single generic
tree-sitter walking engine (see `generic_extractor.py`) driven by a small,
per-language table of node-type sets and field-name conventions, instead of
one hand-written visitor class per language. Adding a language means adding
one `LangSpec` entry (reusing arrays from a similar language where grammars
overlap), not new traversal logic.

Field names were verified against the actual installed grammars (tree-sitter
-language-pack) rather than assumed — see the language-by-language comments.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LangSpec:
    """Table-driven description of one language's tree-sitter grammar shape."""

    key: str                     # stable id, e.g. "python" — also the ContentType value
    ts_language: str              # name passed to tree_sitter_language_pack.get_language
    extensions: tuple[str, ...]   # lowercase file extensions this spec handles, e.g. (".py",)

    # Node types that define a callable (function or method). Both map to the
    # same NodeType.METHOD in the graph schema (Phase 4.4 normalization).
    function_types: frozenset[str]
    # Node types that define a type/namespace container (class/interface/struct).
    class_types: frozenset[str]
    # Node types that represent a call expression.
    call_types: frozenset[str]
    # Node types that represent a member/attribute access (`a.b`, `a->b`, ...)
    # used as the *function* sub-expression of a call, e.g. `obj.method(...)`.
    member_access_types: frozenset[str]
    # Node types for import/using/include statements (captured as text on the
    # Module node; full cross-file import resolution is a later enhancement).
    import_types: frozenset[str] = field(default_factory=frozenset)
    # Node types for decorators/attributes (`@foo`, `[Attribute]`).
    decorator_types: frozenset[str] = field(default_factory=frozenset)
    # Node types treated as "identifier-like" for MinHash/similarity shingling
    # (see ingestion/enrichment/similarity.py) — deliberately excludes string/
    # comment literals so prose doesn't pollute the fingerprint.
    identifier_types: frozenset[str] = frozenset({"identifier"})

    # Field-name conventions (verified per-grammar; see language entries below).
    name_field: str = "name"
    params_field: str = "parameters"
    body_field: str = "body"
    return_type_field: str | None = "return_type"
    bases_field: str | None = None          # class/struct supertypes field, if any
    # Fallback field tried when `name_field` yields nothing — e.g. Rust's
    # `impl_item` (the container methods actually live in) has no "name"
    # field, only "type" (the type being implemented).
    name_field_fallback: str | None = None
    call_function_field: str = "function"
    member_object_field: str = "object"
    member_name_field: str = "attribute"
    # Some grammars (Java-style: Ruby, PHP) put receiver+method-name directly
    # as fields on the call node itself, with no separate member-access child
    # to unwrap. When both are set, this shape is tried before the generic
    # call_function_field/member_access_types shape.
    call_receiver_field: str | None = None
    call_name_field: str | None = None
    # Field on a call node holding its argument list (used for DATA_FLOWS
    # arg→param binding). "arguments" is consistent across every language
    # wired in so far except bash (no argument-list concept).
    args_field: str | None = "arguments"
    # How to extract a bare parameter *name* out of one comma-split parameter
    # fragment of `params_field`'s raw text (used only for DATA_FLOWS arg→
    # param binding — a best-effort heuristic, not a full parser):
    #   "colon" — "name: Type" (Python/TS/Rust/Swift)
    #   "first" — "name Type" no colon (Go)
    #   "last"  — "Type name" (C/C++/C#/Java-style)
    #   "dollar" — "$name" (PHP)
    #   "bare"  — the fragment already is the name (Ruby)
    param_name_style: str = "last"
    # Go-only: method receiver field, used to resolve the owning type name for
    # methods declared outside any class-like body (`func (f *Foo) Bar() {}`).
    receiver_field: str | None = None
    # Node type used for a bare string literal (Python docstring detection).
    docstring_types: frozenset[str] = field(default_factory=frozenset)


LANGUAGE_SPECS: dict[str, LangSpec] = {
    "python": LangSpec(
        key="python", ts_language="python", extensions=(".py",),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"class_definition"}),
        call_types=frozenset({"call"}),
        member_access_types=frozenset({"attribute"}),
        import_types=frozenset({"import_statement", "import_from_statement"}),
        decorator_types=frozenset({"decorator"}),
        identifier_types=frozenset({"identifier"}),
        bases_field="superclasses",
        member_object_field="object", member_name_field="attribute",
        docstring_types=frozenset({"string"}),
        param_name_style="colon",
    ),
    "go": LangSpec(
        key="go", ts_language="go", extensions=(".go",),
        function_types=frozenset({"function_declaration", "method_declaration"}),
        class_types=frozenset({"type_spec"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"selector_expression"}),
        import_types=frozenset({"import_declaration"}),
        identifier_types=frozenset({"identifier", "field_identifier", "type_identifier",
                                    "package_identifier"}),
        return_type_field="result",
        member_object_field="operand", member_name_field="field",
        receiver_field="receiver",
        param_name_style="first",
    ),
    "typescript": LangSpec(
        key="typescript", ts_language="typescript", extensions=(".ts",),
        function_types=frozenset({"function_declaration", "method_definition"}),
        class_types=frozenset({"class_declaration", "interface_declaration"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"member_expression"}),
        import_types=frozenset({"import_statement"}),
        decorator_types=frozenset({"decorator"}),
        identifier_types=frozenset({"identifier", "property_identifier", "type_identifier",
                                    "shorthand_property_identifier"}),
        member_object_field="object", member_name_field="property",
        param_name_style="colon",
    ),
    "tsx": LangSpec(
        key="tsx", ts_language="tsx", extensions=(".tsx",),
        function_types=frozenset({"function_declaration", "method_definition"}),
        class_types=frozenset({"class_declaration", "interface_declaration"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"member_expression"}),
        import_types=frozenset({"import_statement"}),
        decorator_types=frozenset({"decorator"}),
        identifier_types=frozenset({"identifier", "property_identifier", "type_identifier",
                                    "shorthand_property_identifier"}),
        member_object_field="object", member_name_field="property",
        param_name_style="colon",
    ),
    "javascript": LangSpec(
        key="javascript", ts_language="javascript", extensions=(".js", ".jsx", ".mjs", ".cjs"),
        function_types=frozenset({"function_declaration", "method_definition"}),
        class_types=frozenset({"class_declaration"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"member_expression"}),
        import_types=frozenset({"import_statement"}),
        decorator_types=frozenset({"decorator"}),
        identifier_types=frozenset({"identifier", "property_identifier", "shorthand_property_identifier"}),
        return_type_field=None,  # untyped grammar — no return_type field
        member_object_field="object", member_name_field="property",
        param_name_style="colon",
    ),
    "csharp": LangSpec(
        key="csharp", ts_language="csharp", extensions=(".cs",),
        function_types=frozenset({"method_declaration"}),
        class_types=frozenset({"class_declaration", "interface_declaration", "struct_declaration"}),
        call_types=frozenset({"invocation_expression"}),
        member_access_types=frozenset({"member_access_expression"}),
        import_types=frozenset({"using_directive"}),
        decorator_types=frozenset({"attribute"}),
        identifier_types=frozenset({"identifier"}),
        return_type_field=None,   # not exposed as a named field on this grammar version
        bases_field=None,         # `base_list` exists but isn't a named field; skipped
        member_object_field="expression", member_name_field="name",
        param_name_style="last",
    ),
    # ---- Verified batch 2: Rust, Ruby, PHP, C, C++, Bash ----
    "rust": LangSpec(
        key="rust", ts_language="rust", extensions=(".rs",),
        function_types=frozenset({"function_item"}),
        # Methods live inside `impl Foo { ... }`, not inside `struct Foo`; the
        # struct/enum/trait items are included too so standalone type
        # declarations still get a Class node even with no impl block.
        class_types=frozenset({"struct_item", "enum_item", "trait_item", "impl_item"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"field_expression"}),
        import_types=frozenset({"use_declaration"}),
        identifier_types=frozenset({"identifier", "field_identifier", "type_identifier"}),
        return_type_field="return_type",
        name_field_fallback="type",   # impl_item: no "name" field, only "type"
        member_object_field="value", member_name_field="field",
        param_name_style="colon",
    ),
    "ruby": LangSpec(
        key="ruby", ts_language="ruby", extensions=(".rb",),
        function_types=frozenset({"method"}),
        class_types=frozenset({"class", "module"}),
        call_types=frozenset({"call"}),
        member_access_types=frozenset(),   # Ruby's `call` node carries receiver/method directly
        identifier_types=frozenset({"identifier", "constant"}),
        return_type_field=None,
        call_receiver_field="receiver", call_name_field="method",
        param_name_style="bare",
    ),
    "php": LangSpec(
        key="php", ts_language="php", extensions=(".php",),
        function_types=frozenset({"method_declaration", "function_definition"}),
        class_types=frozenset({"class_declaration", "interface_declaration"}),
        call_types=frozenset({"member_call_expression"}),
        member_access_types=frozenset(),   # PHP's member_call_expression carries object/name directly
        import_types=frozenset({"namespace_use_declaration"}),
        identifier_types=frozenset({"name", "variable_name"}),
        return_type_field=None,
        call_receiver_field="object", call_name_field="name",
        param_name_style="dollar",
    ),
    "c": LangSpec(
        key="c", ts_language="c", extensions=(".c", ".h"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"struct_specifier", "enum_specifier", "union_specifier"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset(),   # C has no method-call syntax — only free functions
        import_types=frozenset({"preproc_include"}),
        identifier_types=frozenset({"identifier", "field_identifier", "type_identifier"}),
        name_field="declarator",   # function_definition's name is nested in its declarator
        return_type_field=None,
    ),
    "cpp": LangSpec(
        key="cpp", ts_language="cpp", extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}),
        call_types=frozenset({"call_expression"}),
        member_access_types=frozenset({"field_expression"}),
        import_types=frozenset({"preproc_include", "using_declaration"}),
        identifier_types=frozenset({"identifier", "field_identifier", "type_identifier"}),
        name_field="declarator",   # function_definition's name is nested in its declarator
        return_type_field=None,
        member_object_field="argument", member_name_field="field",
    ),
    "bash": LangSpec(
        key="bash", ts_language="bash", extensions=(".sh", ".bash"),
        function_types=frozenset({"function_definition"}),
        class_types=frozenset(),   # no OOP concept in shell
        call_types=frozenset({"command"}),
        member_access_types=frozenset(),
        identifier_types=frozenset({"word", "command_name", "variable_name"}),
        return_type_field=None,
        call_function_field="name",
        args_field=None,
    ),
}


def spec_for_extension(ext: str) -> LangSpec | None:
    """Return the LangSpec whose `extensions` include `ext` (case-insensitive)."""
    ext = ext.lower()
    for spec in LANGUAGE_SPECS.values():
        if ext in spec.extensions:
            return spec
    return None
