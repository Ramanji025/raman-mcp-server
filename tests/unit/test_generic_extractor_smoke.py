"""Phase 4.1 generic multi-language extraction engine: end-to-end tests
against real tree-sitter grammars for Python/Go/TypeScript/C#."""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_kb.ingestion.langspec import LANGUAGE_SPECS
from mcp_kb.ingestion.parsers.generic_extractor import GenericTreeSitterParser
from mcp_kb.models import ContentType, EdgeType, NodeType, SourceFile

SAMPLES = {
    "python": (
        ".py",
        "import os\n"
        "from typing import List\n\n"
        "class Foo:\n"
        "    def bar(self, x):\n"
        "        y = self.baz(x)\n"
        "        return other_module.helper(y)\n\n"
        "    def baz(self, x):\n"
        "        return x + 1\n",
        ContentType.PYTHON,
    ),
    "go": (
        ".go",
        "package main\n\n"
        "import \"fmt\"\n\n"
        "type Foo struct { Name string }\n\n"
        "func (f *Foo) Bar(x int) int {\n"
        "    y := f.Baz(x)\n"
        "    return other.Helper(y)\n"
        "}\n\n"
        "func (f *Foo) Baz(x int) int { return x + 1 }\n",
        ContentType.GO,
    ),
    "typescript": (
        ".ts",
        "class Foo {\n"
        "    baz(x: number): number { return x + 1; }\n"
        "    bar(x: number): number {\n"
        "        const y = this.baz(x);\n"
        "        return other.helper(y);\n"
        "    }\n"
        "}\n",
        ContentType.TYPESCRIPT,
    ),
    "csharp": (
        ".cs",
        "public class Foo {\n"
        "    private int Baz(int x) { return x + 1; }\n"
        "    public int Bar(int x) {\n"
        "        var y = Baz(x);\n"
        "        return Other.Helper(y);\n"
        "    }\n"
        "}\n",
        ContentType.CSHARP,
    ),
    "rust": (
        ".rs",
        "struct Foo { name: String }\n"
        "impl Foo {\n"
        "    fn baz(&self, x: i32) -> i32 { x + 1 }\n"
        "    fn bar(&self, x: i32) -> i32 {\n"
        "        let y = self.baz(x);\n"
        "        return y + 1;\n"
        "    }\n"
        "}\n",
        ContentType.RUST,
    ),
    "ruby": (
        ".rb",
        "class Foo\n"
        "  def baz(x)\n"
        "    x + 1\n"
        "  end\n"
        "  def bar(x)\n"
        "    y = baz(x)\n"
        "    y + 1\n"
        "  end\n"
        "end\n",
        ContentType.RUBY,
    ),
    "php": (
        ".php",
        "<?php\n"
        "class Foo {\n"
        "    private function baz($x) { return $x + 1; }\n"
        "    public function bar($x) {\n"
        "        $y = $this->baz($x);\n"
        "        return $y;\n"
        "    }\n"
        "}\n",
        ContentType.PHP,
    ),
    "c": (
        ".c",
        "struct Foo { char *name; };\n"
        "int baz(int x) { return x + 1; }\n"
        "int bar(int x) {\n"
        "    int y = baz(x);\n"
        "    return y + 1;\n"
        "}\n",
        ContentType.C,
    ),
    "cpp": (
        ".cpp",
        "class Foo {\n"
        "public:\n"
        "    int baz(int x) { return x + 1; }\n"
        "    int bar(int x) {\n"
        "        int y = this->baz(x);\n"
        "        return y + 1;\n"
        "    }\n"
        "};\n",
        ContentType.CPP,
    ),
    "bash": (
        ".sh",
        "baz() {\n"
        "    echo hello world\n"
        "}\n"
        "bar() {\n"
        "    baz\n"
        "    echo done\n"
        "}\n",
        ContentType.BASH,
    ),
}

# Languages with no OOP/type-container concept (shell) legitimately produce 0
# Class nodes — expected counts are declared per-language rather than assumed.
_EXPECTED_CLASS_COUNT = {lang: (0 if lang == "bash" else 1) for lang in SAMPLES}


@pytest.mark.parametrize("lang_key", sorted(SAMPLES))
def test_generic_extractor_produces_expected_graph_shape(settings, tmp_path: Path, lang_key: str) -> None:
    ext, code, content_type = SAMPLES[lang_key]
    spec = LANGUAGE_SPECS[lang_key]
    parser = GenericTreeSitterParser(settings, spec)

    file_path = tmp_path / f"Foo{ext}"
    file_path.write_text(code, encoding="utf-8")
    source = SourceFile(
        repo="demo-repo", rel_path=file_path.name, abs_path=str(file_path),
        content_type=content_type, sha256="x" * 64, size_bytes=len(code),
    )
    result = parser.parse(source)

    methods = [n for n in result.nodes if n.type == NodeType.METHOD]
    classes = [n for n in result.nodes if n.type in (NodeType.CLASS, NodeType.INTERFACE)]
    files = [n for n in result.nodes if n.type == NodeType.FILE]
    calls_edges = [e for e in result.edges if e.type == EdgeType.CALLS]

    assert len(files) == 1
    # Dedup by id: some grammars legitimately emit >1 AST node for the same
    # logical type (e.g. Rust's `struct Foo` + `impl Foo` both map to one
    # `class:...:Foo` graph node id) — that's a harmless upsert, not a bug.
    assert len({c.id for c in classes}) == _EXPECTED_CLASS_COUNT[lang_key]
    assert len(methods) == 2
    assert len(calls_edges) >= 1, "expected the same-owner call (bar -> baz) to resolve"
    assert any(m.attributes.get("body_tokens") for m in methods), "AST-derived body_tokens missing"
    # Schema normalization: same NodeType/EdgeType regardless of source language.
    assert {m.attributes.get("language") for m in methods} == {lang_key}


def test_generic_extractor_data_flows_parameter_passthrough(settings, tmp_path: Path):
    """outer(self, x) -> self.inner(x) -> inner(self, y): x must flow to y,
    not to `self` (regression test for the call-site self/this offset)."""
    code = (
        "class Foo:\n"
        "    def outer(self, x):\n"
        "        return self.inner(x)\n"
        "    def inner(self, y):\n"
        "        return y + 1\n"
    )
    from mcp_kb.models import ContentType, EdgeType, SourceFile

    spec = LANGUAGE_SPECS["python"]
    parser = GenericTreeSitterParser(settings, spec)
    file_path = tmp_path / "Foo.py"
    file_path.write_text(code, encoding="utf-8")
    source = SourceFile(repo="demo-repo", rel_path="Foo.py", abs_path=str(file_path),
                        content_type=ContentType.PYTHON, sha256="x" * 64, size_bytes=len(code))
    result = parser.parse(source)

    flows = [e for e in result.edges if e.type == EdgeType.DATA_FLOWS]
    assert len(flows) == 1
    nodes_by_id = {n.id: n for n in result.nodes}
    src_node, dst_node = nodes_by_id[flows[0].src], nodes_by_id[flows[0].dst]
    assert src_node.name == "x" and src_node.attributes["method"] == "Foo.outer"
    assert dst_node.name == "y" and dst_node.attributes["method"] == "Foo.inner"
