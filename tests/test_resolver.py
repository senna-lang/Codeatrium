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
