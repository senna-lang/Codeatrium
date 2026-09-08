"""`.codeatrium/ignore` に書かれた gitignore 風パターンでファイルパスを判定する。

verbatim 会話にはトークンやシークレットが混入し得るため、`.env` や `secrets/**` の
ように機微なファイルへ触れた exchange は索引前に弾けるようにする（issue #36）。

対応する記法（`.gitignore` のサブセット）:
  - `#` で始まる行はコメント、空行は無視する。
  - `!` を先頭に置くと、それより前の行のマッチを打ち消す（否定）。
  - `/` を含まないパターンは深さを問わず一致する（`*.pem` は `certs/x.pem` にも一致）。
  - `/` を含むパターンは `.codeatrium/ignore` が置かれたプロジェクトルートからの
    相対パスとして固定される。
  - 末尾の `/` はディレクトリ配下限定（そのディレクトリ自身のパスには一致しない）。
  - ワイルドカードは `*`（`/` をまたがない）・`?`・`[...]`・`**`（`/` をまたぐ）。
  - ルールは記述順で評価し、最後にマッチしたルールの否定有無が結果を決める。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IGNORE_FILENAME = "ignore"


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool


class IgnoreMatcher:
    """1つの ignore ファイルから読み込んだルール集合（不変・イミュータブル）。"""

    def __init__(self, rules: tuple[_Rule, ...] = ()) -> None:
        self._rules = rules

    def matches(self, path: str) -> bool:
        """path（プロジェクトルート相対、区切りは `/` か `\\`）が除外対象なら True。"""
        normalized = PurePosixPath(path.replace("\\", "/")).as_posix().lstrip("/")
        ignored = False
        for rule in self._rules:
            if rule.regex.match(normalized):
                ignored = not rule.negate
        return ignored

    def matches_any(self, paths: Iterable[str]) -> bool:
        """paths のいずれか1つでも除外対象なら True（exchange 全体を除外する判定に使う）。"""
        return any(self.matches(path) for path in paths)


def load_ignore(project_root: Path) -> IgnoreMatcher:
    """`project_root/.codeatrium/ignore` を読み込む。存在しなければ空の matcher を返す。"""
    ignore_path = project_root / ".codeatrium" / IGNORE_FILENAME
    if not ignore_path.exists():
        return IgnoreMatcher()
    rules: list[_Rule] = []
    for raw_line in ignore_path.read_text(encoding="utf-8", errors="replace").splitlines():
        pattern = raw_line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        rules.append(_compile_rule(pattern))
    return IgnoreMatcher(tuple(rules))


def _compile_rule(pattern: str) -> _Rule:
    """1行の gitignore 風パターンを `_Rule`（正規表現＋否定フラグ）へ変換する。"""
    negate = pattern.startswith("!")
    if negate:
        pattern = pattern[1:]
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]

    segments = pattern.split("/")
    # 内部に `/` を含む（=複数 segment になる）パターンは git 同様ルート相対に固定する。
    anchored = anchored or len(segments) > 1

    parts: list[str] = []
    for idx, segment in enumerate(segments):
        if segment == "**":
            is_first = idx == 0
            is_last = idx == len(segments) - 1
            if is_first and is_last:
                parts.append(".*")
            elif is_first:
                parts.append("(?:.*/)?")
            elif is_last:
                parts.append("/.*")
            else:
                parts.append("(?:.*/)?")
            continue
        if idx > 0 and segments[idx - 1] != "**":
            parts.append("/")
        parts.append(_translate_segment(segment))

    body = "".join(parts)
    prefix = "" if anchored else "(?:.*/)?"
    # 末尾が `**`（`/.*` か `.*`）の場合、残り全体を既に吸収済みなので追加の suffix は不要。
    if segments[-1] == "**":
        suffix = ""
    else:
        suffix = "(?:/.*)" if dir_only else "(?:/.*)?"
    return _Rule(regex=re.compile(f"^{prefix}{body}{suffix}$"), negate=negate)


def _translate_segment(segment: str) -> str:
    """`**` を含まない1つのパス segment を正規表現の断片へ変換する。"""
    parts: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        c = segment[i]
        if c == "*":
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            end = segment.find("]", i + 1)
            if end == -1:
                parts.append(re.escape(c))
                i += 1
            else:
                char_class = segment[i : end + 1]
                if char_class.startswith("[!"):
                    char_class = "[^" + char_class[2:]
                parts.append(char_class)
                i = end + 1
        else:
            parts.append(re.escape(c))
            i += 1
    return "".join(parts)
