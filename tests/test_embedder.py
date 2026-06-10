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
