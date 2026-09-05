"""
SymbolResolver のテスト

tree-sitter で Python / TypeScript / Go のシンボルを抽出する。
抽出対象: 関数・クラス・メソッド（symbol_name / symbol_kind / signature / line）
"""

from codeatrium.resolver import Symbol, SymbolResolver

resolver = SymbolResolver()


# ---- Python ----


def test_python_function(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def greet(name: str) -> str:\n    return name\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "greet" in names


def test_python_class(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("class Foo:\n    pass\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Foo" in names


def test_extract_source_matches_extract_for_equivalent_bytes(tmp_path):
    """`extract_source` (used for git-blob resolution) must produce the same
    symbols as `extract` (disk read) for identical content."""
    f = tmp_path / "foo.py"
    source = b"def greet(name: str) -> str:\n    return name\n"
    f.write_bytes(source)

    from_disk = resolver.extract(f)
    from_bytes = resolver.extract_source(source, str(f))

    assert from_disk == from_bytes


def test_extract_source_unsupported_suffix_returns_empty(tmp_path):
    assert resolver.extract_source(b"anything", "foo.rs") == []


def test_python_method(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("class Foo:\n    def bar(self) -> None:\n        pass\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Foo.bar" in names


def test_python_symbol_kind(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def run():\n    pass\n\nclass App:\n    pass\n")
    symbols = resolver.extract(f)
    kinds = {s.symbol_name: s.symbol_kind for s in symbols}
    assert kinds["run"] == "function"
    assert kinds["App"] == "class"


def test_python_signature(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    symbols = resolver.extract(f)
    sig = symbols[0].signature
    assert "add" in sig
    assert "int" in sig


def test_python_line_number(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("# comment\ndef second():\n    pass\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "second")
    assert s.line == 2


def test_python_end_line(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def add(a, b):\n    total = a + b\n    return total\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "add")
    assert s.end_line == 3


def test_python_lang(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def run():\n    pass\n")
    symbols = resolver.extract(f)
    assert symbols[0].lang == ".py"


# ---- TypeScript ----


def test_typescript_function(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("function greet(name: string): string { return name; }\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "greet" in names


def test_typescript_class(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("class Bar {}\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Bar" in names


def test_typescript_method(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("class Bar {\n  baz(x: number): void {}\n}\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Bar.baz" in names


def test_typescript_end_line(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("function greet(name: string): string {\n  return name;\n}\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "greet")
    assert s.end_line == 3


def test_typescript_lang(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("function greet(): void {}\n")
    symbols = resolver.extract(f)
    assert symbols[0].lang == ".ts"


# ---- TypeScript / TSX: arrow function components (design §6.0b) ----


def test_tsx_arrow_function_exported_const(tmp_path):
    f = tmp_path / "Button.tsx"
    f.write_text("export const Button = ({label}) => {\n  return label;\n};\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Button" in names


def test_tsx_arrow_function_plain_const(tmp_path):
    f = tmp_path / "hooks.ts"
    f.write_text("const useCounter = () => {\n  return 1;\n};\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "useCounter" in names


def test_tsx_function_expression_const(tmp_path):
    f = tmp_path / "foo.ts"
    f.write_text("const f = function () {\n  return 1;\n};\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "f" in names


def test_tsx_react_memo_wrapped_function_expression(tmp_path):
    f = tmp_path / "Panel.tsx"
    f.write_text(
        "export const Panel = React.memo(function Panel(props) {\n"
        "  return null;\n"
        "});\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Panel" in names


def test_tsx_react_memo_wrapped_arrow(tmp_path):
    f = tmp_path / "Panel.tsx"
    f.write_text(
        "export const Panel = React.memo((props) => {\n  return null;\n});\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Panel" in names


def test_tsx_forward_ref_wrapped_arrow(tmp_path):
    f = tmp_path / "Input.tsx"
    f.write_text(
        "const Input = React.forwardRef((props, ref) => {\n  return null;\n});\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Input" in names


def test_tsx_export_default_function_still_works(tmp_path):
    f = tmp_path / "Card.tsx"
    f.write_text("export default function Card({title}) {\n  return title;\n}\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Card" in names


def test_tsx_arrow_function_symbol_kind_is_function(tmp_path):
    f = tmp_path / "Button.tsx"
    f.write_text("export const Button = () => {\n  return null;\n};\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "Button")
    assert s.symbol_kind == "function"


def test_tsx_arrow_function_end_line(tmp_path):
    f = tmp_path / "Button.tsx"
    f.write_text("export const Button = () => {\n  return null;\n};\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "Button")
    assert s.end_line == 3


def test_tsx_plain_const_is_not_a_symbol(tmp_path):
    f = tmp_path / "config.ts"
    f.write_text("export const MAX_RETRIES = 3;\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "MAX_RETRIES" not in names


def test_tsx_arrow_function_signature_stops_before_body(tmp_path):
    f = tmp_path / "Button.tsx"
    f.write_text("export const Button = ({label}: Props) => {\n  return label;\n};\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "Button")
    assert s.signature == "export const Button = ({label}: Props) =>"


def test_tsx_nested_const_inside_bare_call_is_not_a_symbol(tmp_path):
    f = tmp_path / "foo.tsx"
    f.write_text(
        "useEffect(() => {\n  const timer = () => {};\n  return timer;\n}, []);\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "timer" not in names


def test_tsx_nested_const_inside_object_literal_is_not_a_symbol(tmp_path):
    f = tmp_path / "foo.tsx"
    f.write_text(
        "const config = {\n"
        "  onClick: () => {\n"
        "    const helper = () => {};\n"
        "    return helper;\n"
        "  },\n"
        "};\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "helper" not in names


def test_tsx_top_level_arrow_component_with_nested_helper(tmp_path):
    f = tmp_path / "Panel.tsx"
    f.write_text(
        "export const Panel = () => {\n"
        "  const renderItem = (item) => item;\n"
        "  return renderItem;\n"
        "};\n"
    )
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert names == ["Panel"]


# ---- Go ----


def test_go_function(tmp_path):
    f = tmp_path / "foo.go"
    f.write_text("package main\nfunc Hello(name string) string { return name }\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Hello" in names


def test_go_method(tmp_path):
    f = tmp_path / "foo.go"
    f.write_text("package main\ntype Foo struct{}\nfunc (f Foo) Bar() {}\n")
    symbols = resolver.extract(f)
    names = [s.symbol_name for s in symbols]
    assert "Foo.Bar" in names


def test_go_lang(tmp_path):
    f = tmp_path / "foo.go"
    f.write_text("package main\nfunc Hello() {}\n")
    symbols = resolver.extract(f)
    s = next(s for s in symbols if s.symbol_name == "Hello")
    assert s.lang == ".go"


# ---- Symbol dataclass ----


def test_symbol_has_file_path(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def run():\n    pass\n")
    symbols = resolver.extract(f)
    assert symbols[0].file_path == str(f)


def test_unsupported_extension_returns_empty(tmp_path):
    f = tmp_path / "foo.rb"
    f.write_text("def hello; end\n")
    symbols = resolver.extract(f)
    assert symbols == []


def test_nonexistent_file_returns_empty(tmp_path):
    f = tmp_path / "nonexistent.py"
    symbols = resolver.extract(f)
    assert symbols == []


def test_returns_symbol_instances(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("def run():\n    pass\n")
    symbols = resolver.extract(f)
    assert isinstance(symbols[0], Symbol)
