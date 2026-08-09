"""
SymbolResolver — tree-sitter でソースファイルからシンボルを抽出する

対応言語: Python / TypeScript / Go
抽出対象: 関数・クラス・メソッド（symbol_name / symbol_kind / signature / line / end_line / file_path / lang）
TypeScript/TSX では `const Button = () => {}` 系のアロー関数・関数式も
`function` として拾う（`React.memo(...)` / `forwardRef(...)` に包まれた形も含む）。

シンボル解決は検索時ではなく蒸留時に一度だけ実行する。
ファイル移動後も記録が残り、検索が高速になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_go as tsgo
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser


@dataclass
class Symbol:
    """tree-sitter で解決されたシンボル情報"""

    symbol_name: str  # "Foo.bar" / "greet" / "Hello"
    symbol_kind: str  # "function" / "class" / "method"
    signature: str  # ソースコードのテキストをそのまま保存（型解決なし）
    line: int  # 1-indexed
    end_line: int  # 1-indexed
    file_path: str
    lang: str  # ファイル拡張子（".py" / ".ts" / ".tsx" / ".go"）


# ---- 言語定義 ----

_LANGUAGES: dict[str, Language] = {
    ".py": Language(tspython.language()),
    ".ts": Language(tstypescript.language_typescript()),
    ".tsx": Language(tstypescript.language_tsx()),
    ".go": Language(tsgo.language()),
}


def _signature(node: Node, source: bytes) -> str:
    """ノードのシグネチャ行（ブロック直前まで）を返す"""
    text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    # ブロック（{, :）以降を除いて先頭1行分を返す
    for sep in (":\n", " {\n", "{\n", "{"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text.split("\n")[0].strip()


class SymbolResolver:
    """ソースファイルからシンボルを抽出する"""

    def extract(self, file_path: Path) -> list[Symbol]:
        """file_path を tree-sitter で解析してシンボルリストを返す"""
        suffix = file_path.suffix.lower()
        language = _LANGUAGES.get(suffix)
        if language is None:
            return []
        if not file_path.exists():
            return []

        source = file_path.read_bytes()
        parser = Parser(language)
        tree = parser.parse(source)

        if suffix == ".py":
            return self._extract_python(tree.root_node, source, str(file_path), suffix)
        if suffix in (".ts", ".tsx"):
            return self._extract_typescript(
                tree.root_node, source, str(file_path), suffix
            )
        if suffix == ".go":
            return self._extract_go(tree.root_node, source, str(file_path), suffix)
        return []

    # ---- Python ----

    def _extract_python(
        self, root: Node, source: bytes, path: str, lang: str
    ) -> list[Symbol]:
        symbols: list[Symbol] = []
        self._walk_python(root, source, path, lang, parent_class=None, symbols=symbols)
        return symbols

    def _walk_python(
        self,
        node: Node,
        source: bytes,
        path: str,
        lang: str,
        parent_class: str | None,
        symbols: list[Symbol],
    ) -> None:
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = source[name_node.start_byte : name_node.end_byte].decode()
                symbols.append(
                    Symbol(
                        symbol_name=class_name,
                        symbol_kind="class",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )
                for child in node.children:
                    self._walk_python(
                        child,
                        source,
                        path,
                        lang,
                        parent_class=class_name,
                        symbols=symbols,
                    )
                return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = source[name_node.start_byte : name_node.end_byte].decode()
                kind = "method" if parent_class else "function"
                full_name = f"{parent_class}.{func_name}" if parent_class else func_name
                symbols.append(
                    Symbol(
                        symbol_name=full_name,
                        symbol_kind=kind,
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )
                return  # メソッド内のネスト関数は追跡しない

        for child in node.children:
            self._walk_python(
                child, source, path, lang, parent_class=parent_class, symbols=symbols
            )

    # ---- TypeScript ----

    # variable_declarator の value がこの型なら関数（コンポーネント）とみなす
    _TS_FUNCTION_VALUE_TYPES = ("arrow_function", "function_expression")

    def _extract_typescript(
        self, root: Node, source: bytes, path: str, lang: str
    ) -> list[Symbol]:
        symbols: list[Symbol] = []
        self._walk_typescript(
            root, source, path, lang, parent_class=None, symbols=symbols
        )
        return symbols

    def _walk_typescript(
        self,
        node: Node,
        source: bytes,
        path: str,
        lang: str,
        parent_class: str | None,
        symbols: list[Symbol],
    ) -> None:
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = source[name_node.start_byte : name_node.end_byte].decode()
                symbols.append(
                    Symbol(
                        symbol_name=class_name,
                        symbol_kind="class",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )
                for child in node.children:
                    self._walk_typescript(
                        child,
                        source,
                        path,
                        lang,
                        parent_class=class_name,
                        symbols=symbols,
                    )
                return

        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = source[name_node.start_byte : name_node.end_byte].decode()
                symbols.append(
                    Symbol(
                        symbol_name=func_name,
                        symbol_kind="function",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )
                return

        if node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node and parent_class:
                method_name = source[name_node.start_byte : name_node.end_byte].decode()
                symbols.append(
                    Symbol(
                        symbol_name=f"{parent_class}.{method_name}",
                        symbol_kind="method",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )
                return

        if node.type == "variable_declarator":
            symbol = self._typescript_variable_symbol(node, source, path, lang)
            if symbol is not None:
                symbols.append(symbol)
                return

        for child in node.children:
            self._walk_typescript(
                child, source, path, lang, parent_class=parent_class, symbols=symbols
            )

    def _typescript_variable_symbol(
        self, node: Node, source: bytes, path: str, lang: str
    ) -> Symbol | None:
        """`const Button = () => {}` 系（§6.0b）を関数シンボルとして拾う

        直接の arrow_function / function_expression だけでなく、
        `React.memo(...)` / `forwardRef(...)` のように呼び出しで包まれた形も対象にする。

        モジュール直下（トップレベル）の宣言のみを対象にする。
        コールバック引数の中の `const helper = () => {}` のようなネストした
        宣言まで拾うと、意図しないシンボルが大量に生まれてしまうため。
        """
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier" or value_node is None:
            return None
        if not self._is_typescript_function_like(value_node):
            return None

        anchor = self._typescript_declaration_anchor(node)
        if anchor.parent is None or anchor.parent.type != "program":
            return None

        func_name = source[name_node.start_byte : name_node.end_byte].decode()
        return Symbol(
            symbol_name=func_name,
            symbol_kind="function",
            signature=_signature(anchor, source),
            line=anchor.start_point[0] + 1,
            end_line=anchor.end_point[0] + 1,
            file_path=path,
            lang=lang,
        )

    def _is_typescript_function_like(self, value_node: Node) -> bool:
        if value_node.type in self._TS_FUNCTION_VALUE_TYPES:
            return True
        if value_node.type == "call_expression":
            args = value_node.child_by_field_name("arguments")
            if args is not None:
                return any(
                    child.type in self._TS_FUNCTION_VALUE_TYPES
                    for child in args.children
                )
        return False

    def _typescript_declaration_anchor(self, declarator: Node) -> Node:
        """signature/line/end_line の基準ノードを返す

        `export const X = ...` の場合は export_statement を、
        そうでなければ lexical_declaration（親）を基準にする。
        """
        declaration = declarator.parent
        if declaration is None:
            return declarator
        export_statement = declaration.parent
        if export_statement is not None and export_statement.type == "export_statement":
            return export_statement
        return declaration

    # ---- Go ----

    def _extract_go(
        self, root: Node, source: bytes, path: str, lang: str
    ) -> list[Symbol]:
        symbols: list[Symbol] = []
        # Go の type_spec から struct 名を収集して receiver マッピングに使う
        type_names: set[str] = set()
        self._collect_go_types(root, source, type_names)
        self._walk_go(root, source, path, lang, type_names, symbols)
        return symbols

    def _collect_go_types(
        self, node: Node, source: bytes, type_names: set[str]
    ) -> None:
        if node.type == "type_spec":
            name_node = node.child_by_field_name("name")
            if name_node:
                type_names.add(
                    source[name_node.start_byte : name_node.end_byte].decode()
                )
        for child in node.children:
            self._collect_go_types(child, source, type_names)

    def _walk_go(
        self,
        node: Node,
        source: bytes,
        path: str,
        lang: str,
        type_names: set[str],
        symbols: list[Symbol],
    ) -> None:
        if node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        symbols.append(
                            Symbol(
                                symbol_name=source[
                                    name_node.start_byte : name_node.end_byte
                                ].decode(),
                                symbol_kind="class",
                                signature=_signature(node, source),
                                line=node.start_point[0] + 1,
                                end_line=node.end_point[0] + 1,
                                file_path=path,
                                lang=lang,
                            )
                        )

        elif node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(
                    Symbol(
                        symbol_name=source[
                            name_node.start_byte : name_node.end_byte
                        ].decode(),
                        symbol_kind="function",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )

        elif node.type == "method_declaration":
            # receiver から型名を取得: (f Foo) → "Foo"
            receiver = node.child_by_field_name("receiver")
            name_node = node.child_by_field_name("name")
            if receiver and name_node:
                receiver_type = self._go_receiver_type(receiver, source)
                method_name = source[name_node.start_byte : name_node.end_byte].decode()
                full_name = (
                    f"{receiver_type}.{method_name}" if receiver_type else method_name
                )
                symbols.append(
                    Symbol(
                        symbol_name=full_name,
                        symbol_kind="method",
                        signature=_signature(node, source),
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=path,
                        lang=lang,
                    )
                )

        for child in node.children:
            self._walk_go(child, source, path, lang, type_names, symbols)

    def _go_receiver_type(self, receiver: Node, source: bytes) -> str:
        """(f Foo) / (f *Foo) から型名 "Foo" を返す"""
        for child in receiver.children:
            if child.type == "parameter_declaration":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        return source[sub.start_byte : sub.end_byte].decode()
                    if sub.type == "pointer_type":
                        for ptr_child in sub.children:
                            if ptr_child.type == "type_identifier":
                                return source[
                                    ptr_child.start_byte : ptr_child.end_byte
                                ].decode()
        return ""
