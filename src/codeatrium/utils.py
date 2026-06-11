"""共通ユーティリティ: プロジェクト横断で再利用される小さなヘルパー関数"""

import hashlib


def sha256(text: str) -> str:
    """テキストの SHA-256 ハッシュ（hex 文字列）を返す"""
    return hashlib.sha256(text.encode()).hexdigest()
