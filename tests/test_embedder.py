"""
Embedder のテスト
モデルロードを避けるため embed() は mock する
"""

from unittest.mock import MagicMock, patch

import pytest

from codeatrium.embedder import Embedder, _try_socket_embed


def test_embedder_returns_384_dim() -> None:
    """embed() が 384次元ベクトルを返す"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    vec = embedder.embed("テストクエリ")
    assert len(vec) == 384


def test_embedder_encode_called_with_text() -> None:
    """embed() がモデルの encode を呼ぶ"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    embedder.embed("テストクエリ")
    embedder._model.encode.assert_called_once()


def test_embedder_returns_float32() -> None:
    """embed() の結果が float32 の numpy 配列"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    vec = embedder.embed("テスト")
    assert vec.dtype == np.float32


def test_embed_via_socket_or_direct_raises_when_model_none() -> None:
    """_ensure_model 後も _model が None なら RuntimeError（assert 撤去・Q4）"""
    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = None
    # _ensure_model を no-op にして _model を None のままにする
    embedder._ensure_model = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        embedder._embed_via_socket_or_direct("テスト", "query", "query: ")


def test_try_socket_embed_chunked_response() -> None:
    """改行終端レスポンスが複数チャンクで届いても再構成される（H5 client）"""
    import numpy as np

    fake_sock = MagicMock()
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = False
    fake_sock.recv.side_effect = [b'{"embedding":[0.1,0.2]}', b"\n", b""]

    mock_path = MagicMock()
    mock_path.exists.return_value = True

    with patch("codeatrium.embedder.socket.socket", return_value=fake_sock):
        vec = _try_socket_embed(mock_path, "query", "hello")

    assert vec is not None
    np.testing.assert_allclose(vec, np.array([0.1, 0.2], dtype=np.float32))



def test_embed_serializes_concurrent_model_encode() -> None:
    """複数スレッドから embed() を並行呼びしても model.encode は直列実行される（issue #16）。

    SentenceTransformer.encode はスレッドセーフを保証しないため、埋め込みサーバーが
    複数クライアント接続を並行処理する際に同時呼び出しが起き得る。同時実行数が
    常に 1 以下（＝直列化されている）ことを検証する。
    """
    import threading
    import time

    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()

    state_lock = threading.Lock()
    concurrent = {"current": 0, "max": 0}

    def fake_encode(_texts, normalize_embeddings=True):
        with state_lock:
            concurrent["current"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["current"])
        time.sleep(0.05)  # 保持時間を延ばして重なりを検出しやすくする
        with state_lock:
            concurrent["current"] -= 1
        return np.zeros((1, 384), dtype="float32")

    embedder._model.encode.side_effect = fake_encode

    threads = [
        threading.Thread(target=embedder.embed, args=(f"query-{i}",)) for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert concurrent["max"] == 1, (
        f"model.encode was called concurrently (max overlap={concurrent['max']}); "
        "expected serialization via Embedder._encode_lock"
    )


def test_find_sock_path_matches_nested_project_root(tmp_path, monkeypatch) -> None:
    """_find_sock_path() は paths.find_project_root()/sock_path() を再利用するため、
    ネストしたプロジェクト（git root 直下ではなくサブディレクトリの .codeatrium/）でも
    find_project_root() が解決するのと同じソケットパスを指す。

    以前は git root を直接 subprocess で解決していたため、ネストプロジェクトでは
    実際のプロジェクトルート（.codeatrium/ の場所）と異なるソケットパスを指し、
    常時コールドスタートになっていた（issue #27）。
    """
    from codeatrium.embedder import _find_sock_path
    from codeatrium.paths import sock_path

    monkeypatch.delenv("CODEATRIUM_NO_SOCK", raising=False)
    monkeypatch.delenv("CODEATRIUM_SOCK_PATH", raising=False)

    git_root = tmp_path / "monorepo"
    nested_project = git_root / "packages" / "app"
    (nested_project / ".codeatrium").mkdir(parents=True)

    monkeypatch.setattr("codeatrium.paths.git_root", lambda: git_root)
    monkeypatch.chdir(nested_project)

    result = _find_sock_path()

    assert result == sock_path(nested_project)
    assert result != sock_path(git_root)


def test_find_sock_path_respects_no_sock_env(tmp_path, monkeypatch) -> None:
    """CODEATRIUM_NO_SOCK が設定されている場合は find_project_root すら呼ばず None を返す
    （サーバー内自己接続デッドロック防止）。
    """
    from codeatrium.embedder import _find_sock_path

    monkeypatch.setenv("CODEATRIUM_NO_SOCK", "1")

    def _boom(*args, **kwargs):
        raise AssertionError("find_project_root must not be called under NO_SOCK")

    monkeypatch.setattr("codeatrium.paths.find_project_root", _boom)

    assert _find_sock_path() is None