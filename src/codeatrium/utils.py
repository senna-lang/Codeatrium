"""共通ユーティリティ: プロジェクト横断で再利用される小さなヘルパー関数"""

import hashlib


def sha256(text: str) -> str:
    """テキストの SHA-256 ハッシュ（hex 文字列）を返す"""
    return hashlib.sha256(text.encode()).hexdigest()


def escape_like(value: str, escape_char: str = "\\") -> str:
    """SQL の LIKE 節に埋め込む前に、ワイルドカード文字をエスケープする。

    `%`（任意長一致）と `_`（任意一文字一致）はユーザー入力にそのまま含まれて
    いても LIKE 側からはワイルドカードとして解釈される（issue #18）。呼び出し側は
    返り値を `%...%` のようなパターンへ組み込み、SQL 側で必ず
    `LIKE ? ESCAPE '\\'`（`escape_char` を渡した場合はその文字）を併記すること。
    エスケープ文字自身が値に含まれる場合に備え、まずエスケープ文字自身を二重化
    してから `%` `_` をエスケープする（順序を変えると二重エスケープが崩れる）。
    """
    escaped = value.replace(escape_char, escape_char * 2)
    escaped = escaped.replace("%", escape_char + "%")
    escaped = escaped.replace("_", escape_char + "_")
    return escaped
