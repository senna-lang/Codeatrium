"""
sentence-transformers / multilingual-e5-small の embedding ラッパー

モデル: intfloat/multilingual-e5-small（384次元・日本語+英語混在対応・CPU動作）

ソケットサーバー方式:
  - .codeatrium/embedder.sock が存在すれば Unix ソケット経由で embed（< 1秒）
  - ソケットなければ直接モデルをロード（~7秒）＋バックグラウンドでサーバー起動
  - 2回目以降は常にソケット経由になるため高速

cold start が問題になった場合は SentenceTransformer(..., backend="onnx") でも対処可能
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


def _loci_python() -> str:
    """venv の Python パスを返す（sys.executable はシステム Python の場合があるため）"""
    # loci CLI と同じ venv の python を使う
    python = Path(sys.executable).parent / "python3"
    if python.exists():
        return str(python)
    return sys.executable


MODEL_NAME = "intfloat/multilingual-e5-small"
CONNECT_TIMEOUT = 2.0  # ソケット接続タイムアウト
RECV_TIMEOUT = 30.0  # 埋め込み受信タイムアウト


def _sock_path_from_env() -> Path | None:
    """環境変数 CODEATRIUM_SOCK_PATH があれば優先使用（テスト用）。
    CODEATRIUM_NO_SOCK=1 の場合はソケット無効（サーバー内自己接続デッドロック防止）。
    """
    import os

    if os.environ.get("CODEATRIUM_NO_SOCK"):
        return None
    p = os.environ.get("CODEATRIUM_SOCK_PATH")
    return Path(p) if p else None


def _find_sock_path() -> Path | None:
    """DB の親ディレクトリの embedder.sock を探す"""
    import os

    if os.environ.get("CODEATRIUM_NO_SOCK"):
        return None
    # .codeatrium/ の場所を git root から解決
    try:
        import subprocess as sp

        result = sp.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()) / ".codeatrium" / "embedder.sock"
    except Exception:
        pass
    return None


def _try_socket_embed(sock_path: Path, req_type: str, text: str) -> np.ndarray | None:
    """ソケットサーバーに接続して embedding を取得する。失敗時は None を返す。"""
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(str(sock_path))
            s.settimeout(RECV_TIMEOUT)
            req = json.dumps({"type": req_type, "text": text}) + "\n"
            s.sendall(req.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            resp = json.loads(buf.split(b"\n")[0])
            if "embedding" in resp:
                return np.array(resp["embedding"], dtype=np.float32)
    except Exception:
        pass
    return None


def _start_server_background(sock_path: Path) -> None:
    """embedder_server をバックグラウンドで起動する"""
    try:
        subprocess.Popen(
            [_loci_python(), "-m", "codeatrium.embedder_server", str(sock_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # サーバーが起動するまで少し待つ
        for _ in range(20):
            if sock_path.exists():
                break
            time.sleep(0.2)
    except Exception:
        pass


class Embedder:
    """multilingual-e5-small の薄いラッパー。

    ソケットサーバーが起動済みなら高速パス（Unix ソケット）を使い、
    なければ直接モデルをロードしてバックグラウンドでサーバーを起動する。
    """

    def __init__(self, sock_path: Path | None = None) -> None:
        self._sock_path: Path | None = (
            _sock_path_from_env() or sock_path or _find_sock_path()
        )
        self._model = None  # 遅延ロード

    def _ensure_model(self) -> None:
        """直接モデルをロードする（ソケット不使用時のフォールバック）"""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(MODEL_NAME)

    def _embed_via_socket_or_direct(
        self, text: str, req_type: str, prefix: str
    ) -> np.ndarray:
        """ソケット優先・なければ直接ロード＋サーバー起動"""
        # ① ソケット経由を試みる
        if self._sock_path is not None:
            vec = _try_socket_embed(self._sock_path, req_type, text)
            if vec is not None:
                return vec

        # ② 直接ロード
        self._ensure_model()
        assert self._model is not None
        result = self._model.encode(
            [f"{prefix}{text}"],
            normalize_embeddings=True,
        )
        vec = result[0].astype(np.float32)

        # ③ バックグラウンドでサーバーを起動（次回から高速化）
        if self._sock_path is not None and not self._sock_path.exists():
            _start_server_background(self._sock_path)

        return vec

    def embed(self, text: str) -> np.ndarray:
        """クエリ用 embedding（query: プレフィックス）"""
        return self._embed_via_socket_or_direct(text, "query", "query: ")

    def embed_passage(self, text: str) -> np.ndarray:
        """インデックス登録用 embedding（passage: プレフィックス）"""
        return self._embed_via_socket_or_direct(text, "passage", "passage: ")
